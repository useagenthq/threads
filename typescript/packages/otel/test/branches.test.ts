import { afterEach, describe, expect, test } from "bun:test";
import {
  type Exporter,
  fakeSandbox,
  LogStore,
  memoryArtifacts,
  openThread,
  type Store,
  type SyncReport,
} from "@threads/core";
import { openBunSqlite } from "@threads/core/bun-sqlite";
import {
  BranchId,
  deleteThread,
  type EventDraft,
  type SqliteDriver,
  storeOf,
  ThreadId,
} from "@threads/core/host";
import { Feed } from "@threads/core/internal/feed";
import { CTX } from "../../core/test/sandbox/context";
import {
  ROOT,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "../../core/test/store/helpers";
import { spanId, traceId } from "../src/ids";
import { accepted, attr, type Collector, collector } from "./collector";
import { exporter, forgetCursors } from "./kit";

// Branch by branch: a fork sends only its own segment, a branch that doesn't read is skipped and
// backed off without holding up the others, and a deletion records what may be lost.

let c: Collector;
afterEach(async () => {
  await c.stop();
});

type Setup = {
  readonly store: Store;
  readonly log: LogStore;
  readonly db: SqliteDriver;
  readonly e: Exporter;
};

const NOW = 1_790_000_000_000;

function setup(): Setup {
  c = collector();
  const db = openBunSqlite(":memory:");
  const artifacts = memoryArtifacts();
  const now = (): number => NOW;
  const log = unwrap(LogStore.open(db, now, artifacts));
  const store = storeOf({ log, artifacts }, { db, now });
  return { store, log, db, e: exporter(store, c.url) };
}

const OTHER = ThreadId.parse("0192a000-0000-7000-8000-0000000000aa");
const OTHER_BRANCH = BranchId.parse("0192b000-0000-7000-8000-0000000000aa");

/** A new thread whose main branch holds `drafts`. */
function thread(
  s: Setup,
  id: ThreadId,
  branch: BranchId,
  drafts: readonly EventDraft[],
): void {
  unwrap(s.log.createBranch(id, branch));
  const writer = unwrap(s.log.acquire(branch, `holder-${branch}`));
  unwrap(writer.append(drafts));
  writer.release();
}

function append(
  s: Setup,
  branch: BranchId,
  drafts: readonly EventDraft[],
): void {
  const writer = unwrap(s.log.acquire(branch, `holder-${branch}-more`));
  unwrap(writer.append(drafts));
  writer.release();
}

async function sync(e: Exporter): Promise<SyncReport> {
  const r = await e.sync();
  if (!r.ok) throw new Error(r.error.message);
  return r.value;
}

const names = (): readonly string[] => accepted(c).map((x) => x.name);

describe("forks", () => {
  test("after thread.fork the parent's spans are never re-sent; the fork's hash its branch", async () => {
    const s = setup();
    const sandbox = fakeSandbox();
    const box = unwrap(await sandbox.create("op-parent", CTX));
    const data = unwrap(await box.snapshot("op-snap", CTX));
    const snap: EventDraft = {
      type: "snapshot",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data,
    };
    thread(s, THREAD, ROOT, [started, userInput("first"), turnCompleted, snap]);
    await sync(s.e);
    const parent = accepted(c).map((x) => x.spanId);
    expect(parent).toHaveLength(1);
    const handle = unwrap(await openThread(s.store, THREAD, { sandbox }));
    const [point] = await handle.forkPoints();
    if (point === undefined) throw new Error("one fork point");
    const child = unwrap(await handle.fork(point));
    const writer = unwrap(s.log.acquire(child.branch, "holder-c"));
    const [opened] = unwrap(writer.append([userInput("again"), turnCompleted]));
    await sync(s.e);
    const sent = accepted(c).slice(1);
    expect(sent).toHaveLength(1);
    const opener = opened?.event.event_id ?? "";
    expect(sent[0]?.spanId).toBe(spanId(child.branch, opener));
    expect(sent[0]?.traceId).toBe(traceId(THREAD, opener));
    expect(sent.map((x) => x.spanId)).not.toContain(parent[0]);
    const first = sent[0];
    expect(first && attr(first, "threads.branch_id")).toBe(child.branch);
  });
});

describe("a branch that doesn't read", () => {
  test("is skipped as log_corrupt while the others export, then backed off until its head moves", async () => {
    const s = setup();
    thread(s, THREAD, ROOT, [started, userInput("healthy"), turnCompleted]);
    thread(s, OTHER, OTHER_BRANCH, [started, userInput("to be broken")]);
    const [row] = s.db.all(
      "SELECT line FROM events WHERE branch_id = ? AND seq = 2",
      [OTHER_BRANCH],
    );
    const original =
      row !== null && typeof row === "object" && "line" in row
        ? row.line
        : null;
    if (!(original instanceof Uint8Array)) throw new Error("the stored line");
    const tampered = new TextEncoder().encode(
      new TextDecoder()
        .decode(original)
        .replace("to be broken", "tampered!!!!"),
    );
    s.db.run("UPDATE events SET line = ? WHERE branch_id = ? AND seq = 2", [
      tampered,
      OTHER_BRANCH,
    ]);
    const first = await sync(s.e);
    expect(first.skipped).toEqual([
      {
        branch_id: OTHER_BRANCH,
        thread_id: OTHER,
        code: "log_corrupt",
        head_seq: 2,
      },
    ]);
    expect(names()).toEqual(["invoke_agent demo"]);
    // Backed off: the next sync doesn't read it again.
    expect((await sync(s.e)).skipped).toEqual([]);
    // Repaired but at the same head: still backed off; an append moves its head and ends that.
    s.db.run("UPDATE events SET line = ? WHERE branch_id = ? AND seq = 2", [
      original,
      OTHER_BRANCH,
    ]);
    expect((await sync(s.e)).skipped).toEqual([]);
    expect(names()).toHaveLength(1);
    append(s, OTHER_BRANCH, [turnCompleted]);
    expect((await sync(s.e)).skipped).toEqual([]);
    expect(names()).toEqual(["invoke_agent demo", "invoke_agent demo"]);
  });

  test("a crashed turn nobody recovers is read once, then not until something appends", async () => {
    const s = setup();
    thread(s, THREAD, ROOT, [started, userInput("crashes mid-turn")]);
    await sync(s.e);
    expect(accepted(c)).toHaveLength(0);
    const feed = await Feed.open(s.store, "otel");
    expect(unwrap(feed.changed())).toEqual([]);
    append(s, ROOT, [turnCompleted]);
    expect(unwrap(feed.changed()).map((b) => b.branch_id)).toEqual([ROOT]);
    await sync(s.e);
    expect(names()).toEqual(["invoke_agent demo"]);
  });
});

describe("deletion losses", () => {
  test("no registered exporter: a deletion inserts no loss row", () => {
    const s = setup();
    thread(s, THREAD, ROOT, [started, userInput("hi"), turnCompleted]);
    unwrap(deleteThread(s.db, "local", THREAD, NOW));
    expect(s.db.all("SELECT * FROM observer_losses", [])).toEqual([]);
  });

  test("counts the events past the cursor, is sent once, and leaves other threads alone", async () => {
    const s = setup();
    thread(s, THREAD, ROOT, [started, userInput("hi"), turnCompleted]);
    thread(s, OTHER, OTHER_BRANCH, [started, userInput("stays")]);
    await sync(s.e);
    append(s, ROOT, [userInput("unsent"), turnCompleted]);
    unwrap(deleteThread(s.db, "local", THREAD, NOW));
    expect(
      s.db.all("SELECT thread_id, unchecked_events FROM observer_losses", []),
    ).toEqual([{ thread_id: THREAD, unchecked_events: 2 }]);
    const report = await sync(s.e);
    expect(report.possiblyLostEvents).toBe(2);
    const lost = accepted(c).filter(
      (x) => x.name === "threads.export.possibly_lost",
    );
    expect(lost).toHaveLength(1);
    expect(lost[0] && attr(lost[0], "threads.unchecked_events")).toBe("2");
    expect((await sync(s.e)).possiblyLostEvents).toBe(0);
    expect(
      accepted(c).filter((x) => x.name === "threads.export.possibly_lost"),
    ).toHaveLength(1);
    expect(
      s.db.all("SELECT count(*) AS n FROM events WHERE branch_id = ?", [
        OTHER_BRANCH,
      ]),
    ).toEqual([{ n: 2 }]);
  });

  test("with the cursor write lost after a 2xx, delivered events are counted too", async () => {
    const s = setup();
    thread(s, THREAD, ROOT, [started, userInput("hi"), turnCompleted]);
    await sync(s.e);
    await forgetCursors(s.store);
    unwrap(deleteThread(s.db, "local", THREAD, NOW));
    expect(
      s.db.all("SELECT unchecked_events FROM observer_losses", []),
    ).toEqual([{ unchecked_events: 3 }]);
  });
});

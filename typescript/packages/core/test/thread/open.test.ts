import { describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { storeOf } from "../../src/agent/sqlite";
import { type EventId, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { fakeSandbox, type SnapshotData } from "../../src/sandbox";
import { openThread, type Thread } from "../../src/thread";
import {
  code,
  fixture,
  ROOT,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "../store/helpers";

const text = (b: Uint8Array): string => new TextDecoder().decode(b);
const bytes = (s: string): Uint8Array => new TextEncoder().encode(s);

/** A parent whose sandbox wrote a file, then snapshotted at a quiescent boundary. */
async function setup() {
  const f = fixture();
  const sandbox = fakeSandbox();
  const box = unwrap(await sandbox.create("op-parent"));
  unwrap(await box.upload("notes.txt", bytes("v1")));
  const data: SnapshotData = unwrap(await box.snapshot("op-snap"));
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
  unwrap(
    writer.append([
      started,
      userInput("write notes"),
      turnCompleted,
      {
        type: "snapshot",
        type_version: 1,
        critical: true,
        actor: { kind: "host" },
        data,
      },
    ]),
  );
  const store = storeOf({ log: f.store, artifacts: f.artifacts });
  const thread = unwrap(await openThread(store, THREAD, { sandbox }));
  return { f, sandbox, box, thread };
}

async function eventAt(thread: Thread, index: number): Promise<EventId> {
  const entry = unwrap(await thread.timeline()).entries[index];
  if (entry === undefined) throw new Error(`no entry ${index}`);
  return entry.event.event_id;
}

describe("openThread", () => {
  test("an unknown thread is not_found", async () => {
    const f = fixture();
    const store = storeOf({ log: f.store, artifacts: f.artifacts });
    const other = ThreadId.parse("0192a000-0000-7000-8000-0000000000ff");
    expect(code(await openThread(store, other))).toBe("not_found");
  });

  test("timeline lists the chain and marks the fork point; forkPoints returns it", async () => {
    const { thread } = await setup();
    const timeline = unwrap(await thread.timeline());
    expect(timeline.entries.map((e) => [e.event.type, e.fork_point])).toEqual([
      ["thread_started", false],
      ["user_input", false],
      ["turn_completed", false],
      ["snapshot", true],
    ]);
    const points = await thread.forkPoints();
    expect(points.map((p) => [p.branch_id, p.seq])).toEqual([[ROOT, 4]]);
  });
});

describe("fork", () => {
  test("restores the snapshot into an isolated child sandbox (F11.1)", async () => {
    const { f, sandbox, box, thread } = await setup();
    const [point] = await thread.forkPoints();
    if (point === undefined) throw new Error("one fork point");
    const child: Thread = unwrap(await thread.fork(point));
    expect(child.branch).not.toBe(ROOT);

    const log = unwrap(f.store.read(child.branch));
    const fork = knownEvents(log).at(-1);
    if (fork?.type !== "fork") throw new Error("the child ends with its fork");
    expect(fork.data.knowledge_policy).toBe("pinned");
    const restored = unwrap(await sandbox.attach(fork.data.sandbox_id ?? ""));
    expect(restored.id).not.toBe(box.id);
    expect(text(unwrap(await restored.download("notes.txt")))).toBe("v1");
    unwrap(await restored.upload("notes.txt", bytes("child")));
    expect(text(unwrap(await box.download("notes.txt")))).toBe("v1");
    expect(unwrap(f.store.ledger.rows()).map((r) => r.state)).toEqual(["live"]);

    // The child continues on its own, like any branch.
    const writer = unwrap(f.store.acquire(child.branch, "holder-c"));
    unwrap(writer.append([userInput("again")]));
  });

  test("records the knowledge policy it was asked for", async () => {
    const { f, thread } = await setup();
    const child = unwrap(
      await thread.fork(await eventAt(thread, 3), { knowledge: "current" }),
    );
    const fork = knownEvents(unwrap(f.store.read(child.branch))).at(-1);
    expect(fork?.type === "fork" ? fork.data.knowledge_policy : "").toBe(
      "current",
    );
  });

  test("off a snapshot boundary it fails and lists nothing", async () => {
    const { f, thread } = await setup();
    const bad = await thread.fork(await eventAt(thread, 1));
    expect(bad.ok ? "ok" : [bad.error.code, bad.error.seq]).toEqual([
      "no_snapshot_boundary",
      2,
    ]);
    expect(unwrap(f.store.ledger.rows())).toEqual([]);
  });

  test("without a sandbox it is sandbox_required and creates nothing", async () => {
    const { f } = await setup();
    const store = storeOf({ log: f.store, artifacts: f.artifacts });
    const thread = unwrap(await openThread(store, THREAD));
    const [point] = await thread.forkPoints();
    if (point === undefined) throw new Error("one fork point");
    expect(code(await thread.fork(point))).toBe("sandbox_required");
    expect(unwrap(f.store.ledger.rows())).toEqual([]);
  });
});

describe("saveCase", () => {
  test("writes the export, its artifacts and case.json, declaring its dependencies", async () => {
    const { thread } = await setup();
    const dir = mkdtempSync(join(tmpdir(), "threads-cases-"));
    const saved = unwrap(
      await thread.saveCase("notes", {
        expect: { must: [{ type: "turn_completed" }] },
        externalEffects: "stub",
        dir,
      }),
    );
    expect(saved).toEqual({ path: join(dir, "notes"), portable: true });
    expect(existsSync(join(saved.path, "log.jsonl"))).toBe(true);
    const manifest: unknown = JSON.parse(
      readFileSync(join(saved.path, "case.json"), "utf8"),
    );
    expect(manifest).toMatchObject({
      external_effects: "stub",
      snapshot: { seq: 4, provider: "fake" },
      dependencies: {
        sandbox_provider: "fake",
        capture_classes: ["filesystem"],
      },
    });
  });

  test("a case without an assertion is refused", async () => {
    const { thread } = await setup();
    const refused = await thread.saveCase("empty", {
      expect: { must: [] },
      externalEffects: "stub",
      dir: mkdtempSync(join(tmpdir(), "threads-cases-")),
    });
    expect(code(refused)).toBe("invalid_request");
  });
});

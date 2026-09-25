import { afterEach, describe, expect, test } from "bun:test";
import { cpSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { HttpAgent, Message } from "@ag-ui/client";
import { type Store, sqlite } from "@threads/core";
import { openStore, tenantStore } from "@threads/core/host";
import { uiThreadId } from "../../src/ui/key";
import {
  alice,
  eventsOf,
  type Harness,
  harness,
  mailer,
  say,
  use,
} from "../kit";
import {
  type Attempt,
  attempt,
  pending,
  succeed,
  uninterrupted,
  user,
} from "./ag-ui-kit";
import { agUi } from "./clients";
import { committed, Gate, inputs, liveModel, lookupAgent } from "./e2e-kit";
import { agUiProjection } from "./stock";

// The stock AG-UI client retrying after a dropped connection: a new runId and the messages the
// HttpAgent then holds. At every frame boundary of a run with a tool call and live text, twice
// in a row, after two earlier turns, during a resumed run, and against a store the thread was
// imported into. Each ends with one user_input and agent.messages equal to an uninterrupted
// client's.

const thread = uiThreadId(alice, "support", "chat-1");
const ask = user("m1", "What is the answer?");
/** The frames of the run below, undropped: ids 1..17 on the wire. */
const FRAMES = 17;
/** Frames the run below streams before its held text response completes. */
const BEFORE_HOLD = 13;

let h: Harness | undefined;
let dirs: string[] = [];
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
  for (const d of dirs) rmSync(d, { recursive: true, force: true });
  dirs = [];
});

/** One runAgent dropped after `n` frames; the held response completes after it drops. */
async function dropped(
  a: HttpAgent,
  log: { dropAfter: number | undefined },
  n: number,
  gate: Gate,
): Promise<void> {
  log.dropAfter = n;
  const run = attempt(a);
  if (n > BEFORE_HOLD) gate.open();
  unfinished(await run);
  gate.open();
}

/** The connection closed before the run's end frame. */
function unfinished(done: Attempt): void {
  expect(done.error).toBeUndefined();
  const last = done.events.at(-1)?.type;
  expect(last === "RUN_FINISHED" || last === "RUN_ERROR").toBe(false);
}

async function equalsUninterrupted(
  a: HttpAgent,
  users: readonly Message[],
  on: Harness,
): Promise<void> {
  expect(agUiProjection(a.messages)).toEqual(
    await uninterrupted(on.host, alice, "chat-1", users),
  );
}

async function oneInput(on: Harness, count = 1): Promise<void> {
  expect(await inputs(on.store, "acme", thread)).toHaveLength(count);
}

describe("AG-UI retries after a drop", () => {
  for (let n = 1; n < FRAMES; n += 1)
    test(`dropped after frame ${n}: the retry replays the one run`, async () => {
      const gate = new Gate();
      h = harness({ agents: { support: lookupAgent(gate) } });
      const { agent: a, log } = agUi(h.host, alice, "chat-1");
      a.addMessage(ask);
      await dropped(a, log, n, gate);
      await succeed(a);
      await oneInput(h);
      await equalsUninterrupted(a, [ask], h);
    });

  test("a double drop: live text, then the replay; the third try completes", async () => {
    const gate = new Gate();
    h = harness({ agents: { support: lookupAgent(gate) } });
    const { agent: a, log } = agUi(h.host, alice, "chat-1");
    a.addMessage(ask);
    log.dropAfter = 11;
    unfinished(await attempt(a));
    log.dropAfter = 2;
    unfinished(await attempt(a));
    gate.open();
    await succeed(a);
    await oneInput(h);
    await equalsUninterrupted(a, [ask], h);
  });

  test("message order: after two earlier turns, the retried turn comes last", async () => {
    const gate = new Gate();
    h = harness({
      agents: { support: lookupAgent(gate, [say("One."), say("Two.")]) },
    });
    const { agent: a, log } = agUi(h.host, alice, "chat-1");
    const first = user("m1", "First");
    const second = user("m2", "Second");
    const third = user("m3", "What is the answer?");
    for (const m of [first, second]) {
      a.addMessage(m);
      await succeed(a);
    }
    a.addMessage(third);
    await dropped(a, log, 11, gate);
    await succeed(a);
    expect(
      a.messages.filter((m) => m.role === "user").map((m) => m.id),
    ).toEqual(["m1", "m2", "m3"]);
    await oneInput(h, 3);
    await equalsUninterrupted(a, [first, second, third], h);
  });

  test("a retry during a resumed run records nothing new and replays it", async () => {
    const sent: string[] = [];
    const gate = new Gate();
    const mail = user("m1", "Mail bob");
    h = harness({
      agents: {
        support: mailer({
          responses: [],
          model: liveModel(
            [use("send_email", { to: "bob" }, "c1"), say("Mailed.")],
            { at: 1, gate },
          ),
          sent,
        }),
      },
    });
    const { agent: a, log } = agUi(h.host, alice, "chat-1");
    a.addMessage(mail);
    await succeed(a);
    const [id] = pending(a);
    if (id === undefined) throw new Error("no interrupt");
    const resume = [
      { interruptId: id, status: "resolved", payload: { decision: "grant" } },
    ] as const;
    log.dropAfter = 2;
    unfinished(await attempt(a, { resume: [...resume] }));
    const retry = succeed(a, { resume: [...resume] });
    await Bun.sleep(20);
    gate.open();
    await retry;
    expect(sent).toEqual(["bob"]);
    expect(pending(a)).toEqual([]);
    const branch = await mainBranch(h.store);
    const decided = (await eventsOf(h.store, "acme", branch)).filter(
      (e) => e.type === "approval_granted",
    );
    expect(decided).toHaveLength(1);
    await oneInput(h);
    await equalsUninterrupted(a, [mail], h);
  });

  test("after an import: the retry is found by the rebuilt ui receipt", async () => {
    const gate = new Gate();
    const [from, to] = [temp(), temp()];
    h = harness({
      store: sqlite(from),
      agents: { support: lookupAgent(gate) },
    });
    const { agent: a, log } = agUi(h.host, alice, "chat-1");
    a.addMessage(ask);
    await dropped(a, log, 11, gate);
    const [run] = await inputs(h.store, "acme", thread);
    if (run === undefined) throw new Error("no run");
    await committed(h.host, alice, thread, String(run.event_id), "ag-ui");
    const bytes = await exportMain(h.store);
    await h.host.stop();
    cpSync(join(from, "artifacts"), join(to, "artifacts"), { recursive: true });
    const store = sqlite(to);
    const imported = await (
      await openStore(tenantStore(store, "acme"))
    ).log.importLog(bytes);
    if (!imported.ok) throw new Error(imported.error.message);
    h = harness({ store, agents: { support: lookupAgent(new Gate()) } });
    const { agent: b } = agUi(h.host, alice, "chat-1");
    b.setMessages(a.messages);
    await succeed(b);
    await oneInput(h);
    await equalsUninterrupted(b, [ask], h);
  });
});

function temp(): string {
  const dir = mkdtempSync(join(tmpdir(), "threads-ui-"));
  dirs.push(dir);
  return dir;
}

async function mainBranch(store: Store): Promise<string> {
  const { log } = await openStore(tenantStore(store, "acme"));
  const main = await log.mainBranch(thread);
  if (!main.ok) throw new Error(main.error.message);
  return main.value;
}

async function exportMain(store: Store): Promise<Uint8Array> {
  const { log } = await openStore(tenantStore(store, "acme"));
  const main = await log.mainBranch(thread);
  if (!main.ok) throw new Error(main.error.message);
  const bytes = await log.exportBranch(main.value);
  if (!bytes.ok) throw new Error(bytes.error.message);
  return bytes.value;
}

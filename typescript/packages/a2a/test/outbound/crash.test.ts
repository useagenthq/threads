import { expect, test } from "bun:test";
import { resume } from "../../../core/src/loop";
import { events } from "../../../core/test/loop/harness";
import { ROOT, unwrap, userInput } from "../../../core/test/store/helpers";
import {
  appendAs,
  CANCELLED,
  drill,
  killAfter,
  lastOf,
  TOOL,
  types,
  without,
} from "./drill";
import { messageIdIn, partner, task } from "./partner";

// The crash drills of 30-uncertainty.md Part 4, on the outbound send. Each kills the process at a
// different point and then asserts, FROM THE LOG, what the next process does. An uncertain outcome
// is never a success and never a silent re-send: it parks (invariant 3).
//
// Every drill also counts the partner's SendMessage requests, because a drill that could not say
// how many times we sent would pass just as well on an empty log.

const ASK = userInput("ask the desk");
const KEY = `${ROOT}:call_1`;

test("killed after effect_begin, before the request left: crash_after_begin, then a park", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => {
    throw new Error("this drill never reaches a send");
  };
  const first = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  const died = await resume(
    first,
    h.artifacts,
    h.config({ onEvent: killAfter(h, "effect_begin") }),
    { input: ASK },
  );
  // The lease went at the begin, so the tool body never ran.
  expect(died).toMatchObject({ kind: "halted", halt: { code: "branch_busy" } });
  expect(p.sends()).toHaveLength(0);
  // The card and the call are durable in the begin's own append (rules 56 and 58).
  expect(types(events(first)).slice(-3)).toEqual([
    "remote_card",
    "remote_call",
    "effect_begin",
  ]);

  // A new process reads the same log and decides from it alone.
  const next = await h.restart();
  const end = await resume(next, h.artifacts, h.config(), { loop: false });
  expect(lastOf(events(next), "effect_unknown")?.data).toMatchObject({
    call_id: "call_1",
    reason: "crash_after_begin",
  });
  // Reconciliation looked; a not_found never proves absence, so it parks rather than re-sends.
  expect(p.requests.filter((r) => r.method === "ListTasks")).toHaveLength(1);
  expect(lastOf(events(next), "parked")?.data).toMatchObject({
    reason: "effect_unknown",
    address: { kind: "effect", id: KEY },
  });
  expect(types(events(next))).not.toContain("effect_resolved");
  expect(types(events(next))).not.toContain("tool_result");
  expect(end).toMatchObject({ kind: "parked" });
  // The whole point: no byte ever left, and the log is what says so.
  expect(p.sends()).toHaveLength(0);
});

test("the answer is lost and the peer hides the task: it parks, and later reconciles", async () => {
  const p = partner();
  const h = await drill(p);
  // The partner creates the task; the answer never reaches us, and a first lookup cannot see it.
  p.hidden = true;
  p.send = () => ({
    kind: "lost",
    task: task("task-7", "TASK_STATE_WORKING"),
    code: "ECONNRESET",
  });
  const first = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  const parked = await resume(first, h.artifacts, h.config(), { input: ASK });
  // A reset after dispatch is uncertainty, never a failed result.
  expect(lastOf(events(first), "effect_unknown")?.data).toMatchObject({
    reason: "transport_error",
  });
  expect(parked).toMatchObject({ kind: "parked" });
  expect(types(events(first))).not.toContain("effect_commit");
  expect(types(events(first))).not.toContain("effect_resolved");
  expect(types(events(first))).not.toContain("tool_result");
  expect(p.sends()).toHaveLength(1);

  // The peer lists it now, with our own messageId in its history.
  p.hidden = false;
  const held = p.tasks.get("task-7");
  p.tasks.set("task-7", {
    ...task("task-7", "TASK_STATE_COMPLETED", "refund 42 was paid"),
    ...(held?.history === undefined ? {} : { history: held.history }),
  });
  const next = await h.restart();
  await resume(next, h.artifacts, h.config(), { loop: false });
  expect(lastOf(events(next), "effect_resolved")?.data).toMatchObject({
    call_id: "call_1",
    outcome: "confirmed_success",
    by: "reconcile",
  });
  const result = lastOf(events(next), "tool_result")?.data;
  expect(result?.is_error).toBe(false);
  expect(result?.preview).toContain("refund 42 was paid");
  // Settled by the peer's own receipt, with nothing sent a second time.
  expect(p.sends()).toHaveLength(1);
});

test("killed after the answer arrived, before it was recorded: reconcile, no second send", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => ({
    kind: "task",
    task: task("task-3", "TASK_STATE_COMPLETED", "refund 42 was paid"),
  });
  const first = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  // The lease goes the moment the partner's answer is on the wire, so the commit cannot land.
  p.onAnswered = async () => {
    h.clock.now += 60_000;
    unwrap(await h.store.acquire(ROOT, "usurper"));
  };
  const died = await resume(first, h.artifacts, h.config(), { input: ASK });
  expect(died).toMatchObject({ kind: "halted", halt: { code: "branch_busy" } });
  expect(p.sends()).toHaveLength(1);
  expect(types(events(first))).not.toContain("effect_commit");
  expect(types(events(first))).not.toContain("tool_result");

  p.onAnswered = undefined;
  const next = await h.restart();
  await resume(next, h.artifacts, h.config(), { loop: false });
  expect(lastOf(events(next), "effect_unknown")?.data.reason).toBe(
    "crash_after_begin",
  );
  expect(lastOf(events(next), "effect_resolved")?.data).toMatchObject({
    outcome: "confirmed_success",
    by: "reconcile",
  });
  expect(lastOf(events(next), "tool_result")?.data.preview).toContain(
    "refund 42 was paid",
  );
  expect(p.sends()).toHaveLength(1);
});

test("the commit, what it observed and the result are one append", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => ({
    kind: "task",
    task: task("task-5", "TASK_STATE_COMPLETED", "already paid"),
  });
  const w = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(w, h.artifacts, h.config(), { input: ASK });
  const log = events(w);
  const commit = log.findIndex((e) => e.type === "effect_commit");
  // One transaction, in this order: a state we observed follows the receipt it was read against
  // (rule 57), so a crash can never leave a commit whose result is lost.
  expect(types(log).slice(commit, commit + 3)).toEqual([
    "effect_commit",
    "remote_task_state",
    "tool_result",
  ]);
  expect(lastOf(log, "effect_commit")?.data.provider_receipt).toBe("task-5");
  expect(lastOf(log, "tool_result")?.data.preview).toContain("already paid");
  expect(p.sends()).toHaveLength(1);
});

test("a peer that answers slowly is working with its task id, and is not sent to again", async () => {
  const p = partner();
  const h = await drill(p);
  // The peer created the task and is still working on it when the deadline passes.
  p.send = () => ({
    kind: "task",
    task: task("task-17", "TASK_STATE_WORKING"),
  });
  const w = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(w, h.artifacts, h.config(), { input: ASK });
  // Slow is not uncertain: the commit already happened, so we hold the peer's receipt.
  expect(lastOf(events(w), "effect_commit")?.data.provider_receipt).toBe(
    "task-17",
  );
  const result = lastOf(events(w), "tool_result")?.data;
  expect(result?.is_error).toBe(false);
  expect(result?.preview).toContain('"status":"working"');
  expect(result?.preview).toContain("task-17");
  expect(types(events(w))).not.toContain("effect_unknown");
  expect(types(events(w))).not.toContain("parked");
  // The model checks later with the status tool; nothing is re-sent.
  expect(p.sends()).toHaveLength(1);
});

test("a refused connection is the one outcome that proves nothing left", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => ({ kind: "refused", code: "ECONNREFUSED" });
  const w = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(w, h.artifacts, h.config(), { input: ASK });
  expect(lastOf(events(w), "effect_resolved")?.data).toMatchObject({
    outcome: "not_sent",
    by: "adapter",
  });
  expect(types(events(w))).not.toContain("parked");
  // One remote_call per call id, whatever happens to its attempts (rule 58).
  expect(types(events(w)).filter((t) => t === "remote_call")).toHaveLength(1);
});

test("a park that reconciliation keeps failing to settle escalates to a person, once", async () => {
  const p = partner();
  const h = await drill(p);
  p.hidden = true;
  p.send = () => ({
    kind: "lost",
    task: task("task-13", "TASK_STATE_WORKING"),
    code: "ECONNRESET",
  });
  const w = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(w, h.artifacts, h.config(), { input: ASK });
  expect(types(events(w))).toContain("parked");
  expect(types(events(w))).not.toContain("park_escalated");

  // A try before the floor still only parks: escalating early would call a person for nothing.
  const soon = await h.restart(60_000);
  await resume(soon, h.artifacts, h.config(), { loop: false });
  expect(types(events(soon))).not.toContain("park_escalated");

  // Past the floor, the next try that cannot settle it calls a person, and resolves nothing.
  const later = await h.restart(300_000);
  await resume(later, h.artifacts, h.config(), { loop: false });
  expect(lastOf(events(later), "park_escalated")?.data).toMatchObject({
    address: { kind: "effect", id: KEY },
  });
  expect(types(events(later))).not.toContain("effect_resolved");

  // A second try does not call again: one escalation per park.
  const again = await h.restart(300_000);
  await resume(again, h.artifacts, h.config(), { loop: false });
  expect(
    types(events(again)).filter((t) => t === "park_escalated"),
  ).toHaveLength(1);
  expect(p.sends()).toHaveLength(1);
});

test("recovery re-checks cancellation before dispatching a call that never began", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => {
    throw new Error("a cancelled call is never sent");
  };
  const first = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(
    first,
    h.artifacts,
    h.config({ onEvent: killAfter(h, "permission_decision") }),
    { input: ASK },
  );
  expect(types(events(first))).not.toContain("effect_begin");
  // A cancel another process recorded while this branch was down.
  await appendAs(h, [CANCELLED]);
  const next = await h.restart();
  await resume(next, h.artifacts, h.config(), { loop: false });
  expect(types(events(next))).not.toContain("effect_begin");
  expect(types(events(next))).not.toContain("remote_call");
  expect(p.sends()).toHaveLength(0);
});

test("recovery re-checks approval before dispatching a call that never began", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => {
    throw new Error("an unapproved call is never sent");
  };
  const ask = { decision: "ask", source: "policy" } as const;
  const first = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  const parked = await resume(
    first,
    h.artifacts,
    h.config({ authorize: () => ask }),
    { input: ASK },
  );
  expect(parked).toMatchObject({ kind: "parked" });
  expect(lastOf(events(first), "parked")?.data.reason).toBe(
    "awaiting_approval",
  );
  expect(types(events(first))).not.toContain("effect_begin");

  // A restart does not read "not started" as permission: it parks on the challenge again.
  const next = await h.restart();
  await resume(next, h.artifacts, h.config({ authorize: () => ask }), {
    loop: false,
  });
  expect(types(events(next))).not.toContain("effect_begin");
  expect(types(events(next))).not.toContain("remote_call");
  expect(p.sends()).toHaveLength(0);
});

test("recovery re-checks policy before dispatching a call that never began", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => {
    throw new Error("a call whose tool is gone is never sent");
  };
  const first = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(
    first,
    h.artifacts,
    h.config({ onEvent: killAfter(h, "permission_decision") }),
    { input: ASK },
  );
  expect(types(events(first))).not.toContain("effect_begin");
  // The tool leaves the set: a removal is a policy change, so an earlier allow is spent.
  await appendAs(h, [without(h.specs, TOOL)]);
  const next = await h.restart();
  await resume(next, h.artifacts, h.config(), { loop: false });
  expect(types(events(next))).not.toContain("effect_begin");
  expect(lastOf(events(next), "tool_result")?.data).toMatchObject({
    is_error: true,
    origin: "not_executed",
  });
  expect(p.sends()).toHaveLength(0);
});

test("every attempt of one call carries one messageId", async () => {
  const p = partner();
  const h = await drill(p);
  p.hidden = true;
  p.send = () => ({
    kind: "lost",
    task: task("task-11", "TASK_STATE_WORKING"),
    code: "ECONNRESET",
  });
  const w = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  await resume(w, h.artifacts, h.config(), { input: ASK });
  const ids = new Set(p.sends().map((r) => messageIdIn(r.body ?? "")));
  expect(ids.size).toBe(1);
  expect(lastOf(events(w), "remote_call")?.data.message_id).toBe([...ids][0]);
});

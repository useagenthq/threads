import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel } from "threadsai";
import { hostTeamIds, type KnownEvent } from "threadsai/host";
import { answers, replyTo } from "../../core/test/team/run-kit";
import {
  alice,
  type Harness,
  harness,
  knownEventsOf,
  say,
  until,
  use,
} from "./kit";
import { assertReplays } from "./team-kit";

// A caller behind the host (lane 29D, section C): a thread in no team addressing a host member by
// name. Every op of a caller is decided by a messagePolicy rule or by default deny — a host team
// grants nothing — and each decision is one message_policy_decided in the caller's own log, which
// only an end-to-end run records. Mirrors Python's tests/host/test_host_members.py.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

/** A host member that replies to the ask it took, then ends its turn idle. */
const billing = agent({
  name: "billing",
  model: answers([
    (request) => replyTo("r1", request, "Paid."),
    () => say("Answered."),
  ]),
});
const hr = agent({ name: "hr", model: scriptedModel({ responses: [] }) });

const caller = (...script: readonly unknown[]) =>
  agent({ name: "support", model: scriptedModel({ responses: [...script] }) });

const decisions = (log: readonly KnownEvent[]) =>
  log.flatMap((e) =>
    e.type === "message_policy_decided"
      ? [[e.data.op, e.data.decision, e.data.source] as const]
      : [],
  );

/** Starts a run of `support` and waits until its log holds `has`. */
async function ran(
  live: Harness,
  input: string,
  has: (log: readonly KnownEvent[]) => boolean,
): Promise<readonly KnownEvent[]> {
  const run = await live.host.startRun(
    { agent: "support", input },
    { principal: alice, idempotencyKey: "k-1" },
  );
  if (!run.ok) throw new Error(run.error.message);
  const branch = run.value.branch_id;
  const log = (): Promise<readonly KnownEvent[]> =>
    knownEventsOf(live.store, alice.tenant, branch);
  await until(async () => has(await log()), 20_000);
  return await log();
}

describe("a caller of a host member", () => {
  test("its ask is decided by a rule, addressed as a caller, and answered", async () => {
    h = harness({
      agents: { support: caller(...ASK), billing },
      members: { billing: {} },
      messagePolicy: [{ from: "support", to: "billing", allow: ["ask"] }],
    });
    await h.host.ready();
    const log = await ran(h, "Is INV-1001 paid?", (events) =>
      events.some((e) => e.type === "tool_result"),
    );
    expect(decisions(log)).toEqual([["ask", "allow", "message_policy"]]);
    const ids = hostTeamIds(alice.tenant);
    const sent = log.find((e) => e.type === "message_sent");
    if (sent?.type !== "message_sent") throw new Error("the ask was sent");
    // A caller's address (rule 52): its thread and branch, and the agent a rule keys on.
    expect(sent.data.envelope.team).toBe(ids.teamId);
    expect(sent.data.envelope.from).toEqual({
      caller: {
        thread_id: sent.thread_id,
        branch_id: sent.branch_id,
        agent: "support",
      },
    });
    const closed = log.find((e) => e.type === "ask_closed");
    if (closed?.type !== "ask_closed") throw new Error("the ask closed");
    expect(closed.data.outcome.status).toBe("answered");
    await assertReplays(h.store, alice.tenant, ids.teamId);
  }, 30_000);

  test("its send with no rule for the target is denied by default and sends nothing", async () => {
    // support may send to hr, so it has the send tool; no rule names billing, so its send
    // there falls through to default deny.
    h = harness({
      agents: { support: caller(...SEND), billing, hr },
      members: { billing: {}, hr: {} },
      messagePolicy: [{ from: "support", to: "hr", allow: ["send"] }],
    });
    await h.host.ready();
    const log = await ran(h, "Tell billing.", (events) =>
      events.some((e) => e.type === "message_policy_decided"),
    );
    expect(decisions(log)).toEqual([["send", "deny", "default"]]);
    expect(log.filter((e) => e.type === "message_sent")).toEqual([]);
  }, 30_000);

  test("its monitor of a host member is denied by default and logged", async () => {
    // No rule may allow monitor towards a host member (setup refuses it), so a caller that has
    // the monitor tool for another agent is denied by default when it names one.
    h = harness({
      agents: { support: caller(...MONITOR), billing, hr },
      members: { billing: {} },
      messagePolicy: [{ from: "support", to: "hr", allow: ["monitor"] }],
    });
    await h.host.ready();
    const log = await ran(h, "Watch billing.", (events) =>
      events.some((e) => e.type === "message_policy_decided"),
    );
    expect(decisions(log)).toEqual([["monitor", "deny", "default"]]);
  }, 30_000);
});

const ASK: readonly unknown[] = [
  use("ask", { to: "billing", question: "Is INV-1001 paid?" }, "c1"),
  say("Billing answered."),
];
const SEND: readonly unknown[] = [
  use("send", { to: "billing", text: "FYI." }, "c1"),
  say("Told billing."),
];
const MONITOR: readonly unknown[] = [
  use("monitor", { member: "billing" }, "c1"),
  say("Watching."),
];

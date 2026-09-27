import { describe, expect, test } from "bun:test";
import { agent, scriptedModel } from "../../src";
import { hostRunner, memberEntry } from "../../src/agent/registry";
import {
  BranchId,
  MemberName,
  Provenance,
  TeamId,
  ThreadId,
} from "../../src/log";
import type { CallContext, Caller } from "../../src/team/call";
import { decision } from "../../src/team/call";
import { TEAM_TOOLS } from "../../src/team/constants";
import type { MessagePolicyRule } from "../../src/team/policy";
import type { PolicyOp } from "../../src/team/request";
import type { MemberRow, TeamRow } from "../../src/team/rows";

// A host team has no grant of its own (lane 29D decision 1): every op of a caller and of a host
// member is decided by a messagePolicy rule or by default deny, never source team. So a caller's
// start, monitor and cancel are always forbidden, and a host member's send to another host member
// needs a rule.

const HOST_TEAM: TeamRow = {
  team_id: TeamId.parse("e34fb000-e656-8122-a748-65b36a92d548"),
  tenant_id: "acme",
  kind: "host",
  lead_thread_id: null,
  team_log_branch_id: BranchId.parse("34c8bb75-7146-872c-a9d6-f1fd978fd58c"),
  closed_at: null,
};

const LEAD_TEAM: TeamRow = {
  ...HOST_TEAM,
  team_id: TeamId.parse("0192c000-0000-7000-8000-000000000001"),
  kind: "lead",
  lead_thread_id: ThreadId.parse("0192a000-0000-7000-8000-0000000000a1"),
};

const row = (role: MemberRow["role"], name: string): MemberRow => ({
  team_id: HOST_TEAM.team_id,
  name: MemberName.parse(name),
  generation: 1,
  role,
  agent: name,
  config_hash: "0".repeat(64),
  thread_id: ThreadId.parse("0192a000-0000-7000-8000-0000000000c1"),
  branch_id: BranchId.parse("0192b000-0000-7000-8000-0000000000c1"),
  state: "idle",
});

const provenance = Provenance.parse({
  principal: { issuer: "api", tenant: "acme", subject: "alice" },
  root_request: {
    thread_id: "0192a000-0000-7000-8000-0000000000d1",
    event_id: "0192e001-0000-7000-8000-000000000002",
  },
  via: [],
});

/** A caller thread of the host team: in no team, so it has no member row. */
const callerOf = (agent: string): Caller => ({
  team: HOST_TEAM,
  row: undefined,
  from: {
    caller: {
      thread_id: ThreadId.parse("0192a000-0000-7000-8000-0000000000d1"),
      branch_id: BranchId.parse("0192b000-0000-7000-8000-0000000000d1"),
      agent,
    },
  },
  agent,
  provenance,
});

const memberOf = (team: TeamRow, role: MemberRow["role"]): Caller => ({
  team,
  row: row(role, "billing"),
  from: {
    tenant: "acme",
    team: team.team_id,
    name: MemberName.parse("billing"),
    generation: 1,
  },
  agent: "billing",
  provenance,
});

/** Only `rules` and the log's own member_started events are read by the decision. */
const ctxWith = (rules: readonly MessagePolicyRule[]): CallContext =>
  ({ chain: { events: [] }, rules }) as unknown as CallContext;

const OPS: readonly PolicyOp[] = ["start", "send", "ask", "monitor", "cancel"];

describe("a host team grants nothing of its own", () => {
  test("every op of a caller with no rule is default deny", () => {
    const ctx = ctxWith([]);
    for (const op of OPS)
      expect(decision(ctx, callerOf("support"), op, "billing")).toEqual({
        source: "default",
      });
  });

  test("a caller's start, monitor and cancel stay denied even with a rule for send and ask", () => {
    const ctx = ctxWith([
      { from: "support", to: "billing", allow: ["send", "ask"] },
    ]);
    const caller = callerOf("support");
    for (const op of ["send", "ask"] as const)
      expect(decision(ctx, caller, op, "billing")).toEqual({
        source: "message_policy",
        rule: { from: "support", to: "billing" },
      });
    for (const op of ["start", "monitor", "cancel"] as const)
      expect(decision(ctx, caller, op, "billing")).toEqual({
        source: "default",
      });
  });

  test("a host member's own send is rule-decided, never granted by the team", () => {
    const acting = memberOf(HOST_TEAM, "host_member");
    expect(decision(ctxWith([]), acting, "send", "hr")).toEqual({
      source: "default",
    });
    expect(
      decision(
        ctxWith([{ from: "billing", to: "hr", allow: ["send"] }]),
        acting,
        "send",
        "hr",
      ),
    ).toEqual({
      source: "message_policy",
      rule: { from: "billing", to: "hr" },
    });
  });

  test("a Phase 1 member's send is still granted by its own team", () => {
    expect(
      decision(ctxWith([]), memberOf(LEAD_TEAM, "member"), "send", "writer"),
    ).toEqual({ source: "team" });
  });
});

/** The team tools a pin offers, in the catalog's order. */
const teamToolsOf = (pinned: { readonly tools: readonly string[] }) =>
  pinned.tools.filter((t) => TEAM_TOOLS.includes(t));

describe("a host member pins reply plus what its rules allow", () => {
  const billing = agent({
    name: "billing",
    model: scriptedModel({ responses: [] }),
  });
  const runnerOf = (rules: readonly MessagePolicyRule[]) => {
    const plain = hostRunner(billing);
    if (plain === undefined) throw new Error("billing has no host runner");
    return rules.length === 0 ? plain : plain.withPolicy({ rules, agents: [] });
  };

  test("with no rule as `from`: reply alone (host_pieces.py BILLING_TOOLS)", async () => {
    expect(teamToolsOf(await runnerOf([]).member.pinned(undefined))).toEqual([
      "reply",
    ]);
  });

  test("with a rule letting it ask another host member: ask and reply", async () => {
    const rules = [
      { from: "billing", to: "hr", allow: ["ask"] },
    ] satisfies readonly MessagePolicyRule[];
    expect(teamToolsOf(await runnerOf(rules).member.pinned(undefined))).toEqual(
      ["ask", "reply"],
    );
  });

  test("a lead's member still gets all seven, which its team's grant covers", async () => {
    const lead = agent({
      name: "lead",
      model: scriptedModel({ responses: [] }),
      team: [billing],
    });
    const entry = memberEntry(lead);
    if (entry === undefined) throw new Error("lead has no member entry");
    expect(teamToolsOf(await entry.pinned(undefined)).toSorted()).toEqual(
      [...TEAM_TOOLS].toSorted(),
    );
  });
});

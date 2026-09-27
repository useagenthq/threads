import { z } from "zod";
import type { EventOf } from "../fold/state";
import { canonicalize, type Provenance, type TeamRefusal } from "../log";
import type { EventDraft } from "../store/admit";
import type { Tx } from "../store/driver";
import type { Chain } from "../verify";
import type { Batch } from "./batch";
import type { ReadText } from "./close";
import type { InvalidDefinition } from "./dynamic";
import { hostTeamIds } from "./host-team";
import type { Envelope, PutText } from "./mail";
import { type MessagePolicyRule, ruleFor } from "./policy";
import { turnProvenance } from "./provenance";
import type { PolicyOp, Request, Target } from "./request";
import {
  type MemberRow,
  memberNamed,
  ownRows,
  refOf,
  type TeamRow,
  teamRow,
} from "./rows";

// One team tool call under the caller's writer (spec/schema/README.md, "Teams", Model tools): its
// policy decision, its refusal and its success are recorded the same way for every op, and the
// call's one tool_result carries the op's result as RFC 8785 JSON. Reference:
// spec/tools/fixtures/ops_request.py.

/** What an op's decision reads, inside the caller's append transaction. */
export type CallContext = {
  readonly tx: Tx;
  /** The caller's committed chain: its turn, the call and what its log recorded. */
  readonly chain: Chain;
  readonly batch: Batch;
  readonly call: EventOf<"tool_call">;
  readonly put: PutText;
  readonly read: ReadText;
  /** The host's messagePolicy rules with the calling agent as `from`; none outside a host. */
  readonly rules: readonly MessagePolicyRule[];
};

/**
 * Who is calling: a member of one team (its row there), or a caller thread in no team, of its
 * tenant's host team (Teams Phase 2). Either way its `from` is the address its mail carries and
 * `agent` is the agent a host rule keys its decision on.
 */
export type Caller = {
  readonly team: TeamRow;
  /** The caller's own member row; undefined for a caller thread, which is in no team. */
  readonly row: MemberRow | undefined;
  readonly from: Envelope["from"];
  readonly agent: string;
  readonly provenance: Provenance;
};

/** Why a call was refused: a team refusal, or one of reply's own. */
export type CallRefusal =
  | TeamRefusal
  | "unknown_ask"
  | "already_replied"
  | "ask_closed";

/** An op's refusal: the op records it as the call's result. */
export type Refusal = {
  readonly refused: CallRefusal;
  /** invalid_definition only: which chosen field, and why. */
  readonly detail?: InvalidDefinition;
};

export const refusal = (
  code: CallRefusal,
  detail?: InvalidDefinition,
): Refusal => ({ refused: code, ...(detail === undefined ? {} : { detail }) });

/** A refusal as the call's tool_result records it. */
export type Refused = {
  readonly status: "refused";
  readonly code: CallRefusal;
  readonly detail?: InvalidDefinition;
};

export function isRefusal(value: unknown): value is Refusal {
  return typeof value === "object" && value !== null && "refused" in value;
}

/**
 * The team the caller acts in: the one it leads (a nested lead starts its own members), the one it
 * is a member of, else — for a thread in no team — its tenant's host team, which it addresses as a
 * caller (Teams Phase 2). Undefined when no team is reachable.
 */
export async function callerOf(ctx: CallContext): Promise<Caller | undefined> {
  const provenance = await turnProvenance(ctx.tx, ctx.chain);
  if (provenance === undefined) return undefined;
  const rows = await ownRows(ctx.tx, ctx.call.thread_id);
  const row = rows.find((r) => r.role === "lead") ?? rows[0];
  if (row === undefined) return await hostCaller(ctx, provenance);
  const team = await teamRow(ctx.tx, row.team_id);
  if (team === undefined) return undefined;
  const from = refOf(team, row);
  return { team, row, from, agent: row.agent, provenance };
}

/** A thread in no team, calling its tenant's host team: its address is `{caller}` (rule 52). */
async function hostCaller(
  ctx: CallContext,
  provenance: Provenance,
): Promise<Caller | undefined> {
  const { teamId } = hostTeamIds(provenance.principal.tenant);
  const team = await teamRow(ctx.tx, teamId);
  const started = ctx.chain.events.find(
    (l) => l.kind === "event" && l.event.type === "thread_started",
  );
  if (team === undefined || started?.kind !== "event") return undefined;
  if (started.event.type !== "thread_started") return undefined;
  const agent = started.event.data.agent_name;
  return {
    team,
    row: undefined,
    from: {
      caller: {
        thread_id: ctx.call.thread_id,
        branch_id: ctx.call.branch_id,
        agent,
      },
    },
    agent,
    provenance,
  };
}

/** The call's mail: `<sender branch_id>:<call_id>`. */
export function callMailId(ctx: CallContext): string {
  return `${ctx.call.branch_id}:${ctx.call.data.call_id}`;
}

/** The request that caused the call's mail: its tool_call. */
export function causalOf(ctx: CallContext): {
  readonly thread_id: string;
  readonly event_id: string;
} {
  return { thread_id: ctx.call.thread_id, event_id: ctx.call.event_id };
}

/** What decided an op: the team's grant, a host rule that adds to it, or default deny. */
export type Decision =
  | { readonly source: "team" | "default" }
  | {
      readonly source: "message_policy";
      readonly rule: { readonly from: string; readonly to: string };
    };

/**
 * The decision order (spec/api.json host.message_policy): the team's grant, then the host's
 * rules, which only add, then default deny.
 */
export function decision(
  ctx: CallContext,
  caller: Caller,
  op: PolicyOp,
  target: string,
): Decision {
  if (granted(ctx, caller, op, target)) return { source: "team" };
  const rule = ruleFor(ctx.rules, caller.agent, target, op);
  if (rule === undefined) return { source: "default" };
  return { source: "message_policy", rule: { from: rule.from, to: rule.to } };
}

/** Records the decision; default deny refuses forbidden. */
export function decide(
  ctx: CallContext,
  op: PolicyOp,
  target: string,
  decided: Decision,
): Refusal | undefined {
  const allow = decided.source !== "default";
  ctx.batch.add({
    type: "message_policy_decided",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      op,
      decision: allow ? "allow" : "deny",
      source: decided.source,
      target,
      call_id: ctx.call.data.call_id,
      ...(decided.source === "message_policy" ? { rule: decided.rule } : {}),
    },
  });
  return allow ? undefined : refusal("forbidden");
}

/** The call's one tool_result: its value as RFC 8785 JSON. */
export function answer(ctx: CallContext, value: unknown): EventDraft {
  return toolResult(ctx.call.data.call_id, value);
}

/** A team call's one tool_result, by call id: its value as RFC 8785 JSON. */
export function toolResult(callId: string, value: unknown): EventDraft {
  const preview = canonicalize(z.json().parse(value));
  if (!preview.ok) throw new Error(preview.error.message);
  return {
    type: "tool_result",
    type_version: 1,
    critical: true,
    actor: { kind: "tool" },
    data: {
      call_id: callId,
      is_error: false,
      completeness: "complete",
      preview: preview.value,
      origin: "executed",
    },
  };
}

/** Records the op's result, or its refusal, as the call's result; returns what it recorded. */
export function recorded(ctx: CallContext, value: Refusal): Refused;
export function recorded<T extends object>(
  ctx: CallContext,
  value: T | Refusal,
): T | Refused;
export function recorded<T extends object>(
  ctx: CallContext,
  value: T | Refusal,
): T | Refused {
  const out = isRefusal(value) ? refusedOf(value) : value;
  ctx.batch.add(answer(ctx, out));
  return out;
}

/** A refusal's recorded value. */
export function refusedOf(value: Refusal): Refused {
  return {
    status: "refused",
    code: value.refused,
    ...(value.detail === undefined ? {} : { detail: value.detail }),
  };
}

/** The call as a team request: its caller sends, and its tool_result records the outcome. */
export async function callRequest(ctx: CallContext): Promise<Request> {
  const caller = await callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  return {
    tx: ctx.tx,
    batch: ctx.batch,
    put: ctx.put,
    team: caller.team,
    from: caller.from,
    provenance: caller.provenance,
    causal: causalOf(ctx),
    mailId: callMailId(ctx),
    self: caller.row,
    decide: (op, target) =>
      decide(ctx, op, target, decision(ctx, caller, op, target)),
    parent: async (startedId) => ({
      thread_id: ctx.call.thread_id,
      branch_id: ctx.call.branch_id,
      event_id: startedId,
      relation: "team_member",
    }),
    refuse: (value) => recorded(ctx, value),
    done: (value) => {
      ctx.batch.add(answer(ctx, value));
      return value;
    },
  };
}

/**
 * The team's Phase 1 grant to a member: a lead starts, a member's starter (its member_started is in
 * the caller's own log) cancels it, and members send, ask and monitor one another. A host team has
 * no grant at all (Teams Phase 2): only a messagePolicy rule allows a caller's or a host member's
 * op, so every such decision's source is message_policy or default.
 */
function granted(
  ctx: CallContext,
  caller: Caller,
  op: PolicyOp,
  target: string,
): boolean {
  const role = caller.row?.role;
  if (role === undefined || role === "host_member") return false;
  if (op === "start") return role === "lead";
  if (op !== "cancel") return true;
  return ctx.chain.events.some(
    (l) =>
      l.kind === "event" &&
      l.event.type === "member_started" &&
      l.event.data.member.name === target,
  );
}

/** A model's target: the member it names, at the generation its own log last recorded. */
export function named(ctx: CallContext, name: string): Target {
  return {
    name,
    row: async () => {
      const caller = await callerOf(ctx);
      if (caller === undefined)
        throw new Error("a team tool call outside a team");
      return addressed(ctx, caller, name);
    },
  };
}

/**
 * The member a model addresses by name, at the generation the caller's own log last recorded for
 * it (its member_started, or a receipt's sender), else the current row's.
 */
export async function addressed(
  ctx: CallContext,
  caller: Caller,
  name: string,
): Promise<MemberRow | Refusal> {
  const row = await memberNamed(ctx.tx, caller.team.team_id, name);
  // A caller holds no generation: it binds the target's current one, read in this transaction.
  const held = caller.row === undefined ? undefined : bound(ctx.chain, name);
  const generation = held ?? row?.generation ?? 0;
  if (row === undefined || generation > row.generation)
    return refusal("unknown_member");
  return generation < row.generation ? refusal("stale_member") : row;
}

function bound(chain: Chain, name: string): number | undefined {
  let seen: number | undefined;
  for (const line of chain.events) {
    if (line.kind !== "event") continue;
    const e = line.event;
    if (e.type === "member_started" && e.data.member.name === name)
      seen = e.data.member.generation;
    else if (e.type === "message_received") {
      const from = e.data.envelope.from;
      if ("name" in from && from.name === name) seen = from.generation;
    }
  }
  return seen;
}

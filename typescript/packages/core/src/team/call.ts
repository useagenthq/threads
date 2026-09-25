import { z } from "zod";
import type { EventOf } from "../fold/state";
import {
  canonicalize,
  type MemberRef,
  type Provenance,
  type TeamRefusal,
} from "../log";
import type { EventDraft } from "../store/admit";
import type { Tx } from "../store/driver";
import type { Chain } from "../verify";
import type { Batch } from "./batch";
import type { ReadText } from "./close";
import type { InvalidDefinition } from "./dynamic";
import type { PutText } from "./mail";
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
};

/** The caller as one team's member: its row there, its ref, its turn's provenance. */
export type Caller = {
  readonly team: TeamRow;
  readonly row: MemberRow;
  readonly ref: MemberRef;
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
 * The team the caller acts in: the one it leads (a nested lead starts its own members), else
 * the one it is a member of. Undefined for a thread in no team.
 */
export async function callerOf(ctx: CallContext): Promise<Caller | undefined> {
  const rows = await ownRows(ctx.tx, ctx.call.thread_id);
  const row = rows.find((r) => r.role === "lead") ?? rows[0];
  const team =
    row === undefined ? undefined : await teamRow(ctx.tx, row.team_id);
  const provenance = await turnProvenance(ctx.tx, ctx.chain);
  if (row === undefined || team === undefined || provenance === undefined)
    return undefined;
  return { team, row, ref: refOf(team, row), provenance };
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

/**
 * Phase 1 policy: the team's grant (source team), else default deny, which refuses forbidden.
 * The decision is recorded either way.
 */
export function decide(
  ctx: CallContext,
  op: PolicyOp,
  target: string,
  allow: boolean,
): Refusal | undefined {
  ctx.batch.add({
    type: "message_policy_decided",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      op,
      decision: allow ? "allow" : "deny",
      source: allow ? "team" : "default",
      target,
      call_id: ctx.call.data.call_id,
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
    from: caller.ref,
    provenance: caller.provenance,
    causal: causalOf(ctx),
    mailId: callMailId(ctx),
    self: caller.row,
    decide: (op, target) =>
      decide(ctx, op, target, granted(ctx, caller, op, target)),
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
 * the caller's own log) cancels it, and members send, ask and monitor one another.
 */
function granted(
  ctx: CallContext,
  caller: Caller,
  op: PolicyOp,
  target: string,
): boolean {
  if (op === "start") return caller.row.role === "lead";
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
  const generation = bound(ctx.chain, name) ?? row?.generation ?? 0;
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

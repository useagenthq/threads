import { z } from "zod";
import type { EventOf } from "../fold/state";
import {
  canonicalize,
  type MemberRef,
  type Provenance,
  type TeamRefusal,
} from "../log";
import type { EventDraft } from "../store/admit";
import type { SqliteDriver } from "../store/driver";
import type { Chain } from "../verify";
import type { Batch } from "./batch";
import type { InvalidDefinition } from "./dynamic";
import type { PutText } from "./mail";
import { turnProvenance } from "./provenance";
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
  readonly db: SqliteDriver;
  /** The caller's committed chain: its turn, the call and what its log recorded. */
  readonly chain: Chain;
  readonly batch: Batch;
  readonly call: EventOf<"tool_call">;
  readonly put: PutText;
};

/** The caller as one team's member: its row there, its ref, its turn's provenance. */
export type Caller = {
  readonly team: TeamRow;
  readonly row: MemberRow;
  readonly ref: MemberRef;
  readonly provenance: Provenance;
};

/** An op's refusal: the op records it as the call's result. */
export type Refusal = {
  readonly refused: TeamRefusal;
  /** invalid_definition only: which chosen field, and why. */
  readonly detail?: InvalidDefinition;
};

export const refusal = (
  code: TeamRefusal,
  detail?: InvalidDefinition,
): Refusal => ({ refused: code, ...(detail === undefined ? {} : { detail }) });

export function isRefusal(value: unknown): value is Refusal {
  return typeof value === "object" && value !== null && "refused" in value;
}

/**
 * The team the caller acts in: the one it leads (a nested lead starts its own members), else
 * the one it is a member of. Undefined for a thread in no team.
 */
export function callerOf(ctx: CallContext): Caller | undefined {
  const rows = ownRows(ctx.db, ctx.call.thread_id);
  const row = rows.find((r) => r.role === "lead") ?? rows[0];
  const team = row === undefined ? undefined : teamRow(ctx.db, row.team_id);
  const provenance = turnProvenance(ctx.db, ctx.chain);
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
  op: "start" | "send",
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
  const preview = canonicalize(z.json().parse(value));
  if (!preview.ok) throw new Error(preview.error.message);
  return {
    type: "tool_result",
    type_version: 1,
    critical: true,
    actor: { kind: "tool" },
    data: {
      call_id: ctx.call.data.call_id,
      is_error: false,
      completeness: "complete",
      preview: preview.value,
      origin: "executed",
    },
  };
}

/** Records the op's result, or its refusal, as the call's result. */
export function recorded(ctx: CallContext, value: unknown): void {
  ctx.batch.add(
    answer(
      ctx,
      isRefusal(value)
        ? {
            code: value.refused,
            ...(value.detail === undefined ? {} : { detail: value.detail }),
            status: "refused",
          }
        : value,
    ),
  );
}

/**
 * The member a model addresses by name, at the generation the caller's own log last recorded for
 * it (its member_started, or a receipt's sender), else the current row's.
 */
export function addressed(
  ctx: CallContext,
  caller: Caller,
  name: string,
): MemberRow | Refusal {
  const row = memberNamed(ctx.db, caller.team.team_id, name);
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

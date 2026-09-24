import { assertNever } from "../assert-never";
import { type EffectStatus, effectKey } from "../fold/state";
import type { EventDraft } from "../store";
import type { Chain } from "../verify";
import { parkNotice } from "./park";
import { turnProvenance } from "./provenance";
import { type AppendContext, settle } from "./settle";

// A failed rebind when the worker resumes a member (spec/schema/README.md, "Teams", A failed
// rebind): the definition a parked, stranded or woken member was started with is gone or changed
// in this process. One append under its writer. A call whose effect is begun or unknown is in
// doubt (invariant 3): the member parks on that effect, as recovery does, and ends only once a
// human settled it. Otherwise each pending call closes from its record (not_executed when its
// effect never began or was settled not done), an open turn closes {error, code}, and the
// member ends failed as any end does.

export type RebindCode = "pin_unavailable" | "pin_mismatch";

type Ctx = AppendContext & { readonly chain: Chain };

const HOST = {
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
} as const;

const DAY = 24 * 60 * 60 * 1000;

export function rebindFailed(ctx: Ctx, code: RebindCode, now: number): void {
  const provenance = turnProvenance(ctx.db, ctx.chain);
  if (provenance === undefined) throw new Error("a member's log has a turn");
  const { fold } = ctx.chain;
  const status = (callId: string): EffectStatus | undefined =>
    fold.effects.get(effectKey(fold, callId, ctx.branchId))?.status;
  const doubt = [...fold.pending].filter((c) => {
    const s = status(c);
    return s === "begun" || s === "unknown";
  });
  if (doubt.length > 0) {
    parkOn(ctx, doubt, status, provenance, now);
    return;
  }
  for (const callId of fold.pending)
    ctx.batch.add(closing(ctx.chain, callId, status(callId), code));
  if (fold.turnOpen)
    ctx.batch.add({
      ...HOST,
      type: "turn_completed",
      data: { reason: "error", code },
    });
  settle(
    {
      ...ctx,
      provenance,
      put: () => {
        throw new Error("a failed rebind's result has no text");
      },
    },
    { status: "failed", error: { code, message: `rebind failed: ${code}` } },
  );
}

/** Parks on each in-doubt effect not parked on yet; the first park of the log notifies. */
function parkOn(
  ctx: Ctx,
  calls: readonly string[],
  status: (callId: string) => EffectStatus | undefined,
  provenance: NonNullable<ReturnType<typeof turnProvenance>>,
  now: number,
): void {
  const { fold, events } = ctx.chain;
  let first = !events.some(
    (l) => l.kind === "event" && l.event.type === "parked",
  );
  for (const callId of calls) {
    if (status(callId) === "begun")
      ctx.batch.add({
        type: "effect_unknown",
        type_version: 1,
        critical: true,
        actor: { kind: "recovery" },
        data: { call_id: callId, reason: "crash_after_begin" },
      });
    const id = effectKey(fold, callId, ctx.branchId);
    if (fold.parked.some((a) => a.kind === "effect" && a.id === id)) continue;
    const eventId = ctx.batch.add({
      type: "parked",
      type_version: 1,
      critical: true,
      actor: { kind: "recovery" },
      data: {
        address: { kind: "effect", id },
        reason: "effect_unknown",
        expires_at: now + DAY,
      },
    });
    if (first)
      parkNotice({ ...ctx, provenance }, { eventId, reason: "effect_unknown" });
    first = false;
  }
}

/** A pending call's tool_result from its record alone: it is never dispatched again. */
function closing(
  chain: Chain,
  callId: string,
  status: EffectStatus | undefined,
  code: RebindCode,
): EventDraft {
  const notExecuted: EventDraft = {
    ...HOST,
    type: "tool_result",
    data: {
      call_id: callId,
      is_error: true,
      completeness: "complete",
      origin: "not_executed",
      preview: `not executed: rebind failed: ${code}`,
    },
  };
  if (status === undefined) return notExecuted;
  const last = chain.events.findLast(
    (l) =>
      l.kind === "event" &&
      (l.event.type === "effect_resolved" ||
        l.event.type === "effect_commit") &&
      l.event.data.call_id === callId,
  );
  const e = last?.kind === "event" ? last.event : undefined;
  if (e?.type === "effect_commit")
    return done(
      callId,
      "materialized_from_commit",
      false,
      "result recorded by its commit",
      e.data.result_ref,
    );
  if (e?.type !== "effect_resolved") return notExecuted;
  const outcome = e.data.outcome;
  switch (outcome) {
    case "safe_to_retry":
    case "not_sent":
    case "assume_not_done":
      return notExecuted;
    case "interrupted":
      return done(
        callId,
        "interrupted",
        true,
        "interrupted: the command may have partly run",
      );
    case "assume_done":
      return done(callId, "executed", false, "assumed done by an approver");
    case "confirmed_success":
      return done(
        callId,
        "executed",
        false,
        "confirmed by an approver",
        e.data.result_ref,
      );
    default:
      return assertNever(outcome);
  }
}

function done(
  callId: string,
  origin: "executed" | "interrupted" | "materialized_from_commit",
  isError: boolean,
  preview: string,
  ref?: {
    readonly sha256: string;
    readonly bytes: number;
    readonly media_type: string;
  },
): EventDraft {
  return {
    ...HOST,
    type: "tool_result",
    data: {
      call_id: callId,
      is_error: isError,
      completeness: "complete",
      origin,
      preview,
      ...(ref === undefined ? {} : { ref }),
    },
  };
}

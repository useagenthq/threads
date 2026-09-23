import { assertNever } from "../assert-never";
import { type EventOf, effectKey } from "../fold/state";
import { draft, RECOVERY } from "./drafts";
import { settleRequested } from "./manual";
import type { Session } from "./session";
import { resolvedResult, settleUnknown } from "./settle";
import { recordOutput } from "./spill";
import type { Halt } from "./types";

// the recovery classifier, run once a new lease is taken and before anything
// new runs. It only settles what is in doubt; its decisions are appended, and a second pass
// appends nothing. The open turn then continues in the loop, which reads the same log.

export async function recover(s: Session): Promise<Halt | undefined> {
  // An open turn with nothing in doubt and nothing pending ends interrupted, unless no
  // model_request follows its last user_input or steer: that input is unsent, so the loop sends it.
  // A requested compaction's side request is cut before the input, so it never sent it.
  const { fold } = s;
  const last = s.events.findLastIndex(
    (e) => e.type === "user_input" || e.type === "steer",
  );
  const answered = s.events
    .slice(Math.max(last, 0))
    .some(
      (e) => e.type === "model_request" && e.data.cause_event_id === undefined,
    );
  if (
    fold.turnOpen &&
    answered &&
    fold.awaiting.size === 0 &&
    fold.pending.size === 0 &&
    fold.parked.length === 0
  )
    return s.append({
      type: "turn_completed",
      type_version: 1,
      critical: true,
      actor: RECOVERY,
      data: { reason: "interrupted" },
    });
  for (const requestId of [...s.fold.awaiting]) {
    const stopped = await recoverRequest(s, requestId);
    if (stopped !== undefined) return stopped;
  }
  for (const callId of [...s.fold.pending]) {
    const stopped = await recoverCall(s, callId);
    if (stopped !== undefined) return stopped;
  }
  return settleRequested(s);
}

/** ask the adapter first; only a final answer settles the attempt. */
async function recoverRequest(
  s: Session,
  requestId: string,
): Promise<Halt | undefined> {
  const model = s.fold.model && s.config.models(s.fold.model);
  const fenced = s.fence();
  if (fenced !== undefined) return fenced;
  const looked =
    model?.lookup === undefined || model.info.lookup === "none"
      ? undefined
      : await model.lookup(`${s.branchId}:${requestId}`, s.modelContext());
  // The fence refused at the lookup's real send point: this writer lost its lease.
  if (looked?.ok === false)
    return { code: "branch_busy", message: looked.error.message };
  const answer = looked?.value;
  // model_response_recovered records the provider's id; a found answer without one can't be
  // recorded, so the attempt stays unknown.
  const found = answer?.status === "found" ? answer.value : undefined;
  const provider_request_id = found?.provider_request_id ?? null;
  if (found !== undefined && provider_request_id !== null) {
    const { content, stop_reason, usage } = found;
    return s.append(
      draft.recovered({
        request_event_id: requestId,
        provider_request_id,
        content: [...content],
        stop_reason,
        usage,
        completeness: "complete",
      }),
    );
  }
  const notSent =
    answer?.status === "not_found" && model?.info.lookup === "final";
  return s.append(
    draft.abandoned(
      {
        request_event_id: requestId,
        provider_outcome: notSent ? "not_sent" : "unknown",
        reason: "crash",
      },
      RECOVERY,
    ),
  );
}

async function recoverCall(
  s: Session,
  callId: string,
): Promise<Halt | undefined> {
  const status = s.fold.effects.get(
    effectKey(s.fold, callId, s.branchId),
  )?.status;
  switch (status) {
    case "committed":
      return materialize(s, callId);
    case "begun": {
      const stopped = s.append(
        draft.effectUnknown(
          { call_id: callId, reason: "crash_after_begin" },
          RECOVERY,
        ),
      );
      return stopped ?? settleUnknown(s, callId, RECOVERY);
    }
    case "unknown":
      return settleUnknown(s, callId, RECOVERY);
    case "resolved":
      return resolved(s, callId);
    case undefined:
      return notStarted(s, callId);
    default:
      return assertNever(status);
  }
}

/**
 * A resolved effect with its call still pending: a terminal resolution closes it from the
 * record; only safe_to_retry, not_sent and assume_not_done may dispatch again.
 */
function resolved(s: Session, callId: string): Halt | undefined {
  const last = s.events.findLast(
    (e): e is EventOf<"effect_resolved"> =>
      e.type === "effect_resolved" && e.data.call_id === callId,
  );
  if (last === undefined)
    throw new Error("a resolved effect has its resolution");
  const outcome = last.data.outcome;
  switch (outcome) {
    case "safe_to_retry":
    case "not_sent":
    case "assume_not_done":
      return notStarted(s, callId);
    case "confirmed_success":
    case "interrupted":
    case "assume_done":
      return resolvedResult(s, last, RECOVERY);
    default:
      return assertNever(outcome);
  }
}

/** effect_commit without tool_result: the result comes from the commit, never a re-run. */
function materialize(s: Session, callId: string): Halt | undefined {
  const commit = s.events.findLast(
    (e) => e.type === "effect_commit" && e.data.call_id === callId,
  );
  if (commit?.type !== "effect_commit")
    throw new Error("a committed effect has its commit");
  const bytes = s.artifacts.get(commit.data.result_ref.sha256);
  if (!bytes.ok)
    return { code: "artifact_missing", message: bytes.error.message };
  const shown = recordOutput(s, callId, new TextDecoder().decode(bytes.value));
  return s.append(
    draft.toolResult(
      {
        call_id: callId,
        is_error: false,
        origin: "materialized_from_commit",
        preview: shown.preview,
        ...(shown.ref === undefined ? {} : { ref: shown.ref }),
      },
      RECOVERY,
    ),
  );
}

/**
 * A call that never began: not started is not permission. The cancellation barrier and the
 * approval state are read again before the loop may dispatch it.
 */
function notStarted(s: Session, callId: string): Halt | undefined {
  const call = s.fold.calls.get(callId);
  if (call?.barrier === true)
    return closed(s, callId, "not_executed", "not executed: cancelled");
  const challenge = [...s.fold.approvals].find(([, a]) => a.callId === callId);
  if (call?.allowed === true || challenge === undefined) return undefined;
  const [id, approval] = challenge;
  if (approval.consumed)
    return closed(s, callId, "denied", "denied: approval denied");
  const requested = s.events.findLast(
    (e): e is EventOf<"approval_requested"> =>
      e.type === "approval_requested" && e.data.challenge_id === id,
  );
  if (requested !== undefined && requested.data.expires_at <= s.now())
    return closed(s, callId, "denied", "denied: approval expired");
  if (s.fold.parked.some((a) => a.kind === "approval" && a.id === id))
    return undefined;
  return s.append(
    draft.parked(
      { address: { kind: "approval", id }, reason: "awaiting_approval" },
      RECOVERY,
    ),
  );
}

function closed(
  s: Session,
  callId: string,
  origin: "not_executed" | "denied",
  preview: string,
): Halt | undefined {
  return s.append(
    draft.toolResult(
      { call_id: callId, is_error: true, origin, preview },
      RECOVERY,
    ),
  );
}

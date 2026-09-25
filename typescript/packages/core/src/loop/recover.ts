import { assertNever } from "../assert-never";
import {
  type EventOf,
  effectKey,
  loopParked,
  loopPending,
} from "../fold/state";
import { ok } from "../result";
import { draft, RECOVERY } from "./drafts";
import { lookedUp } from "./lookup";
import { settleRequested } from "./manual";
import type { Session } from "./session";
import { resolvedResult, settleUnknown } from "./settle";
import { recordOutput } from "./spill";
import { cancelRequested, owedCalls } from "./turn";
import type { Halt } from "./types";

// the recovery classifier, run once a new lease is taken and before anything
// new runs. It only settles what is in doubt; its decisions are appended, and a second pass
// appends nothing. The open turn then continues in the loop, which reads the same log.

export async function recover(s: Session): Promise<Halt | undefined> {
  // An open turn with nothing in doubt and nothing pending ends interrupted, unless no
  // model_request follows its last user_input or steer (that input is unsent, so the loop sends
  // it) or a cancel is durable in it (the loop carries the cancel out). A requested compaction's
  // side request is cut before the input, so it never sent it.
  const { fold } = s;
  // A cancelled an older writer recorded without its turn_completed: the turn ends cancelled.
  if (fold.turnOpen && cancelAnswered(s))
    return s.append({
      type: "turn_completed",
      type_version: 1,
      critical: true,
      actor: RECOVERY,
      data: { reason: "cancelled" },
    });
  // The turn's opener (an input, a woken, a receipt) or a later steer.
  const last = s.events.findLastIndex(
    (e) => e.seq === fold.turnStart || e.type === "steer",
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
    loopPending(fold).length === 0 &&
    loopParked(fold).length === 0 &&
    !cancelOpen(s) &&
    !answeredSinceRequest(s) &&
    // A response's calls are recorded next, never left without results.
    (owedCalls(s.events, fold)?.parts.length ?? 0) === 0
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
  for (const callId of loopPending(s.fold)) {
    const stopped = await recoverCall(s, callId);
    if (stopped !== undefined) return stopped;
  }
  return settleRequested(s);
}

/**
 * A question settled (answered or expired) since the last request: the turn waited for it, and the
 * loop sends the answer on, so nothing was interrupted.
 */
function answeredSinceRequest(s: Session): boolean {
  const request = s.events.findLastIndex((e) => e.type === "model_request");
  return s.events
    .slice(request + 1)
    .some((e) => e.type === "resumed" && e.data.address.kind === "input");
}

/** A cancel_requested in the open turn that no cancelled has answered yet. */
function cancelOpen(s: Session): boolean {
  const cancel = cancelRequested(s.events, s.fold);
  return (
    cancel !== undefined &&
    !s.events.some(
      (e) =>
        e.type === "cancelled" && e.data.request_event_id === cancel.event_id,
    )
  );
}

/** The open turn's cancelled, already recorded. */
function cancelAnswered(s: Session): boolean {
  const start = s.events.findLastIndex((e) => e.type === "user_input");
  return s.events.slice(Math.max(start, 0)).some((e) => e.type === "cancelled");
}

/** ask the adapter first; only a final answer settles the attempt. */
async function recoverRequest(
  s: Session,
  requestId: string,
): Promise<Halt | undefined> {
  const model = s.fold.model && s.config.models(s.fold.model);
  const fenced = await s.fence();
  if (fenced !== undefined) return fenced;
  const lookup = model?.info.lookup === "none" ? undefined : model?.lookup;
  const looked =
    lookup === undefined
      ? undefined
      : await lookedUp(
          () => lookup(`${s.branchId}:${requestId}`, s.modelContext()),
          (reason) => ok({ status: "unknown", reason }),
        );
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
      const stopped = await s.append(
        draft.effectUnknown(
          { call_id: callId, reason: "crash_after_begin" },
          RECOVERY,
        ),
      );
      return stopped ?? (await settleUnknown(s, callId, RECOVERY));
    }
    case "unknown":
      return settleUnknown(s, callId, RECOVERY);
    case "resolved":
      return resolved(s, callId);
    case undefined:
      return closeGoneOrDispatch(s, callId, false);
    default:
      return assertNever(status);
  }
}

/**
 * A resolved effect with its call still pending: a terminal resolution closes it from the
 * record; only safe_to_retry, not_sent and assume_not_done may dispatch again.
 */
async function resolved(s: Session, callId: string): Promise<Halt | undefined> {
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
      return closeGoneOrDispatch(s, callId, true);
    case "confirmed_success":
    case "interrupted":
    case "assume_done":
      return resolvedResult(s, last, RECOVERY);
    default:
      return assertNever(outcome);
  }
}

/** effect_commit without tool_result: the result comes from the commit, never a re-run. */
async function materialize(
  s: Session,
  callId: string,
): Promise<Halt | undefined> {
  const commit = s.events.findLast(
    (e) => e.type === "effect_commit" && e.data.call_id === callId,
  );
  if (commit?.type !== "effect_commit")
    throw new Error("a committed effect has its commit");
  const bytes = await s.artifacts.get(commit.data.result_ref.sha256);
  if (!bytes.ok)
    return { code: "artifact_missing", message: bytes.error.message };
  const shown = await recordOutput(
    s,
    callId,
    new TextDecoder().decode(bytes.value),
  );
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
async function notStarted(
  s: Session,
  callId: string,
): Promise<Halt | undefined> {
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
      {
        address: { kind: "approval", id },
        reason: "awaiting_approval",
        ...(requested === undefined
          ? {}
          : { expires_at: requested.data.expires_at }),
      },
      RECOVERY,
    ),
  );
}

/**
 * A call the loop would dispatch never runs without a tool: one made to a tool not in the set
 * closes not_executed, and so does one that never began whose tool a later tools_changed removed
 * (a removal is a policy change, so an earlier allow never dispatches a call past it).
 */
function closeGoneOrDispatch(
  s: Session,
  callId: string,
  began: boolean,
): Promise<Halt | undefined> {
  const spec = s.fold.calls.get(callId)?.spec;
  if (spec === undefined)
    return closed(
      s,
      callId,
      "not_executed",
      `not executed: unknown tool ${callName(s, callId)}`,
    );
  if (!began && !s.fold.tools.some((t) => t.name === spec.name))
    return closed(
      s,
      callId,
      "not_executed",
      `not executed: ${spec.name} was removed from the tool set`,
    );
  return notStarted(s, callId);
}

function callName(s: Session, callId: string): string {
  const call = s.events.findLast(
    (e) => e.type === "tool_call" && e.data.call_id === callId,
  );
  return call?.type === "tool_call" ? call.data.name : callId;
}

function closed(
  s: Session,
  callId: string,
  origin: "not_executed" | "denied",
  preview: string,
): Promise<Halt | undefined> {
  return s.append(
    draft.toolResult(
      { call_id: callId, is_error: true, origin, preview },
      RECOVERY,
    ),
  );
}

import type { EventOf, Fold } from "../fold/state";
import { sameAddress } from "../fold/state";
import { acceptsRecorded } from "../tools/ask-user";
import { invalid, type Violation } from "./violation";

// Rules 7, 8, 9 (call_id, approval consumption), 11, 13, 19, 25, 46 and 47.

/** Rule 9: a call_id is used once. */
export function checkToolCall(fold: Fold, e: EventOf<"tool_call">): Violation {
  return fold.calls.has(e.data.call_id)
    ? invalid(`call_id ${e.data.call_id} is already used`)
    : undefined;
}

/** Rules 7 and 25. */
export function checkToolResult(
  fold: Fold,
  e: EventOf<"tool_result">,
): Violation {
  const { call_id: callId, origin } = e.data;
  if (!fold.pending.has(callId))
    return invalid(`tool_result for ${callId}, which has no pending tool_call`);
  if (origin !== "answered") return undefined;
  if (!asking(fold, callId))
    return invalid(
      `answered tool_result for ${callId} without an open question`,
    );
  const ask = fold.asks.get(callId);
  return ask === undefined ||
    ask === "invalid" ||
    acceptsRecorded(ask, e.data.preview)
    ? undefined
    : invalid(`the answer to ${callId} is none of its options`);
}

function asking(fold: Fold, callId: string): boolean {
  const question = { kind: "input", id: callId } as const;
  return fold.parked.some((address) => sameAddress(address, question));
}

/** Rule 46: a park on {kind: input} names a pending ask_user call the question rules accept. */
export function checkQuestionPark(fold: Fold, e: EventOf<"parked">): Violation {
  const { address } = e.data;
  if (address.kind !== "input") return undefined;
  const ask = fold.asks.get(address.id);
  if (!fold.pending.has(address.id) || ask === undefined)
    return invalid(`a question park on ${address.id}, no pending ask_user`);
  return ask === "invalid"
    ? invalid(`ask_user ${address.id} breaks the question rules`)
    : undefined;
}

/** Rule 47: a rejected answer names an open question. */
export function checkAnswerRejected(
  fold: Fold,
  e: EventOf<"answer_rejected">,
): Violation {
  return asking(fold, e.data.call_id)
    ? undefined
    : invalid(`answer_rejected for ${e.data.call_id}, no open question`);
}

/** Rule 7: a late result follows a deferred placeholder, once. */
export function checkLateResult(
  fold: Fold,
  e: EventOf<"tool_result_late">,
): Violation {
  const call = fold.calls.get(e.data.call_id);
  return call?.deferred === true && !call.late
    ? undefined
    : invalid(
        `tool_result_late for ${e.data.call_id} without a deferred placeholder`,
      );
}

/** Rule 8: authorized, not barred by cancellation, and never for a read_only call. */
export function checkEffect(
  fold: Fold,
  e: EventOf<
    "effect_begin" | "effect_commit" | "effect_unknown" | "effect_resolved"
  >,
): Violation {
  const callId = e.data.call_id;
  const call = fold.calls.get(callId);
  if (call === undefined)
    return e.type === "effect_begin"
      ? invalid(`effect_begin for ${callId}, which has no tool_call`)
      : undefined;
  if (call.spec?.effect_class === "read_only")
    return invalid(`read_only call ${callId} writes no effect events`);
  if (e.type !== "effect_begin") return undefined;
  if (!call.allowed)
    return invalid(`effect_begin for ${callId} without permission or approval`);
  return call.barrier
    ? invalid(`effect_begin for ${callId} after cancel_requested`)
    : undefined;
}

/** Rules 9 and 13: an approval consumes its open challenge once, and matches it. */
export function checkApproval(
  fold: Fold,
  e: EventOf<"approval_granted" | "approval_denied">,
): Violation {
  const challenge = fold.approvals.get(e.data.challenge_id);
  if (challenge?.consumed === true)
    return invalid(`challenge ${e.data.challenge_id} is already consumed`);
  const matches =
    challenge !== undefined &&
    challenge.callId === e.data.call_id &&
    challenge.argsHash === e.data.args_hash;
  return matches
    ? undefined
    : {
        code: "approval_mismatch",
        message: `${e.type} does not match open challenge ${e.data.challenge_id}`,
      };
}

/** Rule 11: cancelled never leaves an effect begun or unknown. */
export function checkCancelled(fold: Fold): Violation {
  const unsettled = [...fold.effects.values()].find(
    (effect) => effect.status === "begun" || effect.status === "unknown",
  );
  return unsettled === undefined
    ? undefined
    : invalid(
        `cancelled while the effect of ${unsettled.callId} is ${unsettled.status}`,
      );
}

type Edit = EventOf<"context_edited">["data"]["edits"][number];

/** Rule 19: edits name recorded results; redactions cut text parts on character boundaries. */
export function checkContextEdit(
  fold: Fold,
  e: EventOf<"context_edited">,
): Violation {
  for (const edit of e.data.edits) {
    const violation = checkEdit(fold, edit);
    if (violation !== undefined) return violation;
  }
  return undefined;
}

function checkEdit(fold: Fold, edit: Edit): Violation {
  const result = fold.calls.get(edit.call_id)?.result;
  if (result === undefined)
    return invalid(`context_edited names ${edit.call_id}, which has no result`);
  if (edit.action === "clear") return undefined;
  const parts = result.data.content ?? [
    { type: "text", text: result.data.preview },
  ];
  const part = parts[edit.part];
  if (part?.type !== "text")
    return invalid(
      `redaction part ${edit.part} of ${edit.call_id} is not text`,
    );
  const bytes = new TextEncoder().encode(part.text);
  const inside = (offset: number): boolean =>
    offset <= bytes.length && ((bytes[offset] ?? 0) & 0xc0) !== 0x80;
  const bad = edit.spans.find(
    (span) => span.start > span.end || !inside(span.start) || !inside(span.end),
  );
  return bad === undefined
    ? undefined
    : invalid(
        `redaction span ${bad.start}..${bad.end} of ${edit.call_id} is out of bounds`,
      );
}

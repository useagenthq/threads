import {
  type EventLine,
  type EventOf,
  effectKey,
  type Fold,
} from "../fold/state";
import { invalid, type Violation } from "./violation";

// Rules 56, 57 and 58: an A2A call's stored request, the begin it belongs to, and the partner
// task states observed against it.

/** Rule 56: nothing comes between a remote_call and the effect_begin of its call. */
export function checkRemoteBegin(fold: Fold, line: EventLine): Violation {
  const callId = fold.remoteBegin;
  if (callId === undefined) return undefined;
  const begins =
    line.kind === "event" &&
    line.event.type === "effect_begin" &&
    line.event.data.call_id === callId;
  return begins
    ? undefined
    : invalid(`remote_call ${callId} is not followed by its effect_begin`);
}

/** Rule 58: one remote_call per call_id, so every attempt sends the bytes it stored. */
export function checkRemoteCall(
  fold: Fold,
  e: EventOf<"remote_call">,
): Violation {
  return fold.remoteCalls.has(e.data.call_id)
    ? invalid(`call_id ${e.data.call_id} already has a remote_call`)
    : undefined;
}

/** Rule 57: a partner's task state is observed only for a call we hold a receipt for. */
export function checkRemoteState(
  fold: Fold,
  e: EventOf<"remote_task_state">,
): Violation {
  const callId = e.data.call_id;
  if (!fold.remoteCalls.has(callId))
    return invalid(`remote_task_state for ${callId}, which has no remote_call`);
  const effect = fold.effects.get(effectKey(fold, callId, e.branch_id));
  return effect?.status === "committed"
    ? undefined
    : invalid(`remote_task_state for ${callId}, whose effect has no commit`);
}

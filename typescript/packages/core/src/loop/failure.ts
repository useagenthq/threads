import type { EventOf } from "../fold/state";
import type { RunErrorCode } from "./types";

// How a turn that ended failed reads to the caller: its RunErrorCode and what to do next. A run
// reports it as RunResult.failed, a team member as its failed MemberResult. The same text as
// Python's `FAILED_MESSAGES`.

type Ended = EventOf<"turn_completed">["data"];

const MESSAGES = {
  error:
    "the model request failed; the thread's timeline has the provider's error",
  interrupted:
    "the model's reply was cut off before it finished; run the thread again to continue",
  model_unavailable:
    "no model could be reached, including any fallbacks; check the provider's status and your API key",
  context_exhausted:
    "the conversation no longer fits the model's context window, even after compaction; start a new thread or use a model with a larger window",
  max_output:
    "the model hit its output token cap; raise the model's max output tokens or ask for a shorter answer",
  max_turns:
    "the run reached the agent's turn limit; raise max turns or split the task",
  output_invalid:
    "the model's answer failed the output schema on every retry; raise output retries or loosen the schema",
  input_denied:
    "a hook or policy refused the input, so the model was not called",
  stop_hook_limit:
    "a stop hook kept the run going past its limit; check when the hook asks to continue",
} as const;

function isFailedReason(
  reason: Ended["reason"],
): reason is keyof typeof MESSAGES {
  return Object.hasOwn(MESSAGES, reason);
}

/** A failed end's code and message; undefined for an end that is not a failure. */
export function failureOf(end: Ended):
  | {
      readonly code: RunErrorCode;
      readonly message: string;
    }
  | undefined {
  const { reason, code } = end;
  const message = isFailedReason(reason) ? MESSAGES[reason] : MESSAGES.error;
  // Only a team member's rebind ends a turn this way, and it records its own result.
  if (
    code === "pin_unavailable" ||
    code === "pin_mismatch" ||
    code === "setup_failed"
  )
    throw new Error(`a run's turn can't end ${code}: only a member rebinds`);
  if (code !== undefined) return { code, message };
  if (reason === "error" || reason === "interrupted")
    return { code: "model_error", message };
  return isFailedReason(reason) ? { code: reason, message } : undefined;
}

import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import { recordCalls } from "./calls";
import { runCalls } from "./dispatch";
import { draft } from "./drafts";
import { missingCandidate } from "./output";
import { contextPolicy } from "./policy";
import { endTurn, requestTurn } from "./request";
import type { Session } from "./session";
import { nextStep, type Response, turnEvents } from "./turn";
import type { Halt } from "./types";

// The loop: every step is decided from the committed log (turn.ts), so a fresh run, a resumed
// one and a recovered one take the same path. It runs until the branch is idle or parked.

export type LoopEnd =
  | { readonly kind: "idle" }
  | { readonly kind: "parked" }
  | { readonly kind: "halted"; readonly halt: Halt };

const CONTINUE =
  "Output limit reached. Continue exactly where you stopped. Do not repeat earlier output.";

export async function runLoop(s: Session): Promise<LoopEnd> {
  for (;;) {
    const seq = s.fold.seq;
    const step = nextStep(s.events, s.fold);
    let stopped: Halt | undefined;
    switch (step.kind) {
      case "idle":
      case "parked":
        return step;
      case "cancel":
        stopped = cancel(s, step.request);
        break;
      case "calls":
        stopped = await runCalls(s);
        break;
      case "request":
        stopped = await requestTurn(s);
        break;
      case "respond":
        stopped = respond(s, step.response);
        break;
      case "end_turn":
        stopped = endTurn(s, "end_turn");
        break;
      default:
        return assertNever(step);
    }
    if (stopped !== undefined) return { kind: "halted", halt: stopped };
    // Every step appends; one that doesn't would spin forever, which is a bug.
    if (s.fold.seq === seq)
      throw new Error(`loop step ${step.kind} made no progress`);
  }
}

function respond(s: Session, response: Response): Halt | undefined {
  if (response.data.content.some((p) => p.type === "tool_use"))
    return recordCalls(s, response);
  if (response.data.stop_reason === "max_tokens") return continuation(s);
  return missingCandidate(s);
}

/** ask to continue, up to max_output_continuations per turn, then max_output. */
function continuation(s: Session): Halt | undefined {
  const asked = turnEvents(s.events).filter(
    (e) => e.type === "injected" && e.data.text === CONTINUE,
  ).length;
  if (asked >= contextPolicy(s.fold.policy).max_output_continuations)
    return endTurn(s, "max_output");
  return s.append(
    draft.injected({
      source: "recovery",
      trust: "trusted_instruction",
      origin: { id: "max_output" },
      text: CONTINUE,
    }),
  );
}

/**
 * calls that never began close as not_executed; cancelled is appended only
 * once no effect is begun or unknown (those park instead, through recovery).
 */
function cancel(
  s: Session,
  request: EventOf<"cancel_requested">,
): Halt | undefined {
  for (const callId of [...s.fold.pending]) {
    const stopped = s.append(
      draft.toolResult(
        {
          call_id: callId,
          is_error: true,
          origin: "not_executed",
          preview: "not executed: cancelled",
        },
        { kind: "host" },
      ),
    );
    if (stopped !== undefined) return stopped;
  }
  return (
    s.append(draft.cancelled({ request_event_id: request.event_id })) ??
    endTurn(s, "cancelled")
  );
}

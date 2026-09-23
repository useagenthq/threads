import type { z } from "zod";
import { assertNever } from "../assert-never";
import { type EventOf, type ParkAddress, responseText } from "../fold/state";
import type { BranchId, KnownEvent, ThreadId } from "../log";
import type { LoopEnd, RunErrorCode } from "../loop";
import { turnEvents } from "../loop/turn";
import type { Store } from "./sqlite";

// RunResult (spec/api.json): a discriminated union on status, read from the run's log.

/** The data of a thread at one branch; openThread returns the full Thread handle. */
export type ThreadRef = {
  readonly id: ThreadId;
  readonly branch: BranchId;
  readonly store: Store;
};

export type RunResult<Output = string> =
  | {
      readonly status: "completed";
      readonly output: Output;
      readonly thread: ThreadRef;
    }
  | {
      readonly status: "parked";
      readonly reason: EventOf<"parked">["data"]["reason"];
      readonly pending: readonly ParkAddress[];
      readonly thread: ThreadRef;
    }
  | { readonly status: "cancelled"; readonly thread: ThreadRef }
  | {
      readonly status: "failed";
      readonly error: { readonly code: RunErrorCode; readonly message: string };
      readonly thread: ThreadRef;
    }
  | {
      readonly status: "budget_exhausted";
      readonly budget: EventOf<"budget_exceeded">["data"];
      readonly thread: ThreadRef;
    }
  | {
      readonly status: "handed_off";
      readonly thread: ThreadRef;
      readonly to_thread: ThreadRef;
    };

type Reason = EventOf<"turn_completed">["data"]["reason"];

/** The accepted value, or the final text; `decode` is the API boundary for Output. */
export type Decode<Output> = (
  text: string,
  accepted: z.core.util.JSONType | undefined,
) => Output;

export function runResult<Output>(
  end: LoopEnd,
  events: readonly KnownEvent[],
  parked: readonly ParkAddress[],
  thread: ThreadRef,
  decode: Decode<Output>,
): RunResult<Output> {
  if (end.kind === "halted")
    return { status: "failed", error: end.halt, thread };
  if (end.kind === "parked") {
    const last = events.findLast((e) => e.type === "parked");
    const reason =
      last?.type === "parked" ? last.data.reason : "effect_unknown";
    return { status: "parked", reason, pending: parked, thread };
  }
  const turn = turnEvents(events);
  const done = turn.findLast((e) => e.type === "turn_completed");
  if (done?.type !== "turn_completed")
    throw new Error("an idle run ended its turn");
  // A typed error end (the capability pre-check) reports its own code.
  if (done.data.code !== undefined)
    return failed(done.data.code, done.data.reason, thread);
  return ended(done.data.reason, turn, thread, decode);
}

function ended<Output>(
  reason: Reason,
  turn: readonly KnownEvent[],
  thread: ThreadRef,
  decode: Decode<Output>,
): RunResult<Output> {
  switch (reason) {
    case "end_turn":
      return { status: "completed", output: output(turn, decode), thread };
    case "cancelled":
      return { status: "cancelled", thread };
    case "budget_exhausted": {
      const budget = turn.findLast((e) => e.type === "budget_exceeded");
      if (budget?.type !== "budget_exceeded")
        throw new Error("budget_exhausted records why");
      return { status: "budget_exhausted", budget: budget.data, thread };
    }
    case "handoff":
      throw new Error("handoffs are not part of this release");
    case "error":
    case "interrupted":
      return failed("model_error", reason, thread);
    case "max_turns":
    case "stop_hook_limit":
    case "max_output":
    case "context_exhausted":
    case "output_invalid":
    case "input_denied":
    case "model_unavailable":
      return failed(reason, reason, thread);
    default:
      return assertNever(reason);
  }
}

function failed<Output>(
  code: RunErrorCode,
  reason: Reason,
  thread: ThreadRef,
): RunResult<Output> {
  return {
    status: "failed",
    error: { code, message: `the turn ended ${reason}` },
    thread,
  };
}

function output<Output>(
  turn: readonly KnownEvent[],
  decode: Decode<Output>,
): Output {
  const accepted = turn.findLast(
    (e) => e.type === "output_validated" && e.data.outcome === "accepted",
  );
  const response = turn.findLast(
    (e) => e.type === "model_response" || e.type === "model_response_recovered",
  );
  const text =
    response?.type === "model_response" ||
    response?.type === "model_response_recovered"
      ? responseText(response.data.content)
      : "";
  return decode(
    text,
    accepted?.type === "output_validated" &&
      accepted.data.outcome === "accepted"
      ? accepted.data.value
      : undefined,
  );
}

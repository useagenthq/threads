import type { z } from "zod";
import { assertNever } from "../assert-never";
import { type EventOf, type ParkAddress, responseText } from "../fold/state";
import type { BranchId, KnownEvent, ThreadId } from "../log";
import type { LoopEnd, RunErrorCode } from "../loop";
import { failureOf } from "../loop/failure";
import { type RunEnd, runEnd } from "../reduce/run-end";
import type { Thread } from "../thread/handle";
import type { Store } from "./sqlite";

// RunResult (spec/api.json): a discriminated union on status, read from the run's log.

/** The data of a thread at one branch: what run({thread}) takes. A Thread handle is one. */
export type ThreadRef = {
  readonly id: ThreadId;
  readonly branch: BranchId;
  readonly store: Store;
};

export type RunResult<Output = string> =
  | {
      readonly status: "completed";
      readonly output: Output;
      readonly thread: Thread;
    }
  | {
      readonly status: "parked";
      readonly reason: EventOf<"parked">["data"]["reason"];
      readonly pending: readonly ParkAddress[];
      readonly thread: Thread;
    }
  | { readonly status: "cancelled"; readonly thread: Thread }
  | {
      readonly status: "failed";
      readonly error: { readonly code: RunErrorCode; readonly message: string };
      readonly thread: Thread;
    }
  | {
      readonly status: "budget_exhausted";
      readonly budget: EventOf<"budget_exceeded">["data"];
      readonly thread: Thread;
    }
  | {
      readonly status: "handed_off";
      readonly thread: Thread;
      readonly to_thread: Thread;
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
  thread: Thread,
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
  const request = events.findLast((e) => e.type === "user_input");
  if (request === undefined) throw new Error("an idle run had an input");
  return endedRun(runEnd(events, request.event_id), thread, decode);
}

/**
 * The result of a run that ended (spec/schema/README.md, "Run completion"): completed with its
 * last answer, or the outcome of the run turn that ended otherwise.
 */
export function endedRun<Output>(
  run: RunEnd,
  thread: Thread,
  decode: Decode<Output>,
): RunResult<Output> {
  // A cancel while the run waited on its children ends it outside any turn.
  if (run.status === "cancelled" && run.turn.length === 0)
    return { status: "cancelled", thread };
  const done = run.turn.at(-1);
  if (done?.type !== "turn_completed")
    throw new Error("an ended run ended its turn");
  // A typed error end (the capability pre-check) reports its own code.
  const failure = failureOf(done.data);
  if (failure !== undefined)
    return { status: "failed", error: failure, thread };
  return ended(done.data.reason, run.turn, thread, decode);
}

function ended<Output>(
  reason: Reason,
  turn: readonly KnownEvent[],
  thread: Thread,
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
      // run() answers a handed-off thread itself, with the target it started.
      throw new Error("a handoff's result names its target thread");
    case "error":
    case "interrupted":
    case "max_turns":
    case "stop_hook_limit":
    case "max_output":
    case "context_exhausted":
    case "output_invalid":
    case "input_denied":
    case "model_unavailable":
      throw new Error("failureOf reports every failed end");
    default:
      return assertNever(reason);
  }
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

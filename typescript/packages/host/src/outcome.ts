import type { ParkAddress } from "@threads/core/host";
import {
  type BranchId,
  type EventId,
  endedRun,
  type JsonValue,
  type KnownEvent,
  type RunResult,
  runEnd,
  runResult,
  type Thread,
  type ThreadId,
} from "@threads/core/host";
import type { z } from "zod";

// host-api RunOutcome: RunResult as data, with ids where the library has a Thread handle. The
// SSE result of a run is read from the log (Host.subscribe reads, it never runs), so the same
// run answers the same outcome to every subscriber, before and after a restart.

type Json = z.infer<typeof JsonValue>;
type Ids = { readonly thread_id: ThreadId; readonly branch_id: BranchId };

export type RunOutcome =
  | (Ids & { readonly status: "completed"; readonly output: Json })
  | (Ids & {
      readonly status: "parked";
      readonly reason: Extract<RunResult<Json>, { status: "parked" }>["reason"];
      readonly pending: readonly ParkAddress[];
    })
  | (Ids & { readonly status: "cancelled" })
  | (Ids & {
      readonly status: "failed";
      readonly error: Extract<RunResult<Json>, { status: "failed" }>["error"];
    })
  | (Ids & {
      readonly status: "budget_exhausted";
      readonly budget: Extract<
        RunResult<Json>,
        { status: "budget_exhausted" }
      >["budget"];
    })
  | (Ids & { readonly status: "handed_off"; readonly to_thread_id: ThreadId });

/** The HTTP form of a library RunResult. */
export function toOutcome(result: RunResult<Json>): RunOutcome {
  const ids = { thread_id: result.thread.id, branch_id: result.thread.branch };
  switch (result.status) {
    case "completed":
      return { ...ids, status: "completed", output: result.output };
    case "parked":
      return {
        ...ids,
        status: "parked",
        reason: result.reason,
        pending: result.pending,
      };
    case "cancelled":
      return { ...ids, status: "cancelled" };
    case "failed":
      return { ...ids, status: "failed", error: result.error };
    case "budget_exhausted":
      return { ...ids, status: "budget_exhausted", budget: result.budget };
    case "handed_off":
      return {
        ...ids,
        status: "handed_off",
        to_thread_id: result.to_thread.id,
      };
  }
}

const decode = (text: string, accepted: Json | undefined): Json =>
  accepted ?? text;

/**
 * The outcome of the run whose user_input is `runId`, once the log shows it ended
 * (spec/schema/README.md, "Run completion"): after its last wake turn once every background
 * child it spawned reported, at the first turn that ended otherwise, or parked. Undefined while
 * it is still going.
 */
export function outcomeFromLog(
  events: readonly KnownEvent[],
  runId: EventId,
  parked: readonly ParkAddress[],
  thread: Thread,
): RunOutcome | undefined {
  if (!events.some((e) => e.event_id === runId)) return undefined;
  const run = runEnd(events, runId);
  switch (run.status) {
    case "running":
      return undefined;
    case "parked":
      return toOutcome(
        runResult({ kind: "parked" }, events, parked, thread, decode),
      );
    case "handed_off": {
      const handoff = run.turn.findLast((e) => e.type === "handoff");
      if (handoff?.type !== "handoff")
        throw new Error("a handed-off turn records its handoff");
      return {
        thread_id: thread.id,
        branch_id: thread.branch,
        status: "handed_off",
        to_thread_id: handoff.data.to_thread_id,
      };
    }
    default:
      return toOutcome(endedRun(run, thread, decode));
  }
}

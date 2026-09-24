import type { KnownEvent } from "../log";
import type { Fold } from "./state";
import { runKey, sameRun, type TurnRun } from "./team";

// Background wakes (spec/schema/README.md, "Background wakes"; rule 32): the late results of the
// current append, and the one wake condition both the validator and the writer read. The run of
// each turn and of each background spawn is the team fold's tracker (TurnRun).

export type WakeFold = {
  /** tool_result_late event ids (to their call_id) since the last event that was neither a
   * tool_result_late nor an agent_finished: the late results of one append. */
  readonly trailingLate: Map<string, string>;
};

export function emptyWake(): WakeFold {
  return { trailingLate: new Map() };
}

/** Advances the wake bookkeeping past one event that passed validate_next. A woken opens a turn
 * whose run the team fold has already set. */
export function applyWake(
  fold: { turnOpen: boolean; readonly wake: WakeFold },
  e: KnownEvent,
): void {
  if (e.type === "woken") fold.turnOpen = true;
  if (e.type === "tool_result_late")
    fold.wake.trailingLate.set(e.event_id, e.data.call_id);
  else if (e.type !== "agent_finished") fold.wake.trailingLate.clear();
}

/** The run that spawned each background call, or undefined for a call with no known run. */
export function spawnRuns(
  fold: Fold,
  calls: readonly string[],
): readonly (TurnRun | undefined)[] {
  return calls.map((call) => fold.team.spawns.get(call));
}

/**
 * Why late results of these background calls must not wake the thread now, or undefined when
 * they may (rule 32): no turn is open; the thread is not cancelled, has not handed off and has
 * not ended; they were spawned by one run, which has not ended otherwise; and no thread or tree
 * cancel request followed any of their spawns.
 */
export function wakeBar(
  fold: Fold,
  calls: readonly string[],
): string | undefined {
  if (fold.turnOpen) return "woken while a turn is open";
  if (fold.cancelled) return "woken after the thread was cancelled";
  if (fold.handedOff) return "woken after a handoff";
  if (fold.team.ended) return "an ended member's log opens a turn";
  const runs = spawnRuns(fold, calls);
  const [first] = runs;
  if (first === undefined || runs.some((r) => r === undefined))
    return "a woken cause is not the late result of a background child";
  if (!runs.every((r) => r !== undefined && sameRun(r, first)))
    return "woken names children of more than one run";
  if (fold.team.endedRuns.has(runKey(first)))
    return "woken for a run that ended otherwise";
  if (calls.some((call) => fold.team.barred.has(call)))
    return "woken for a child a thread or tree cancel followed";
  return undefined;
}

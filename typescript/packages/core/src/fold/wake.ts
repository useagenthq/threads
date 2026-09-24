import { type KnownEvent, principalKey } from "../log";

// Background wakes (spec/schema/README.md, "Background wakes"): which run each turn belongs to,
// which run spawned each background child, and the late results of the current append.

/** A run: the principal and root request of the turn opener that started it. */
export type Run = { readonly principal: string; readonly root: string };

export type WakeFold = {
  /** The run of the open (or last) turn. */
  run: Run | undefined;
  /** The run that spawned each background spawn_agent call, by call_id. */
  readonly spawnRuns: Map<string, Run>;
  /** tool_result_late event ids (to their call_id) since the last event that was neither a
   * tool_result_late nor an agent_finished: the late results of one append. */
  readonly trailingLate: Map<string, string>;
};

export function emptyWake(): WakeFold {
  return { run: undefined, spawnRuns: new Map(), trailingLate: new Map() };
}

/** Advances the wake bookkeeping past one event that passed validate_next. A woken opens a turn
 * whose run is the one that spawned the child it names. */
export function applyWake(
  fold: { turnOpen: boolean; readonly wake: WakeFold },
  e: KnownEvent,
): void {
  const { wake } = fold;
  if (e.type === "woken") {
    fold.turnOpen = true;
    const first = e.data.causes[0];
    const call = first === undefined ? undefined : wake.trailingLate.get(first);
    wake.run = call === undefined ? undefined : wake.spawnRuns.get(call);
  } else if (e.type === "user_input" && e.actor.principal !== undefined)
    wake.run = { principal: principalKey(e.actor.principal), root: e.event_id };
  else if (
    e.type === "agent_spawned" &&
    e.data.mode === "background" &&
    wake.run
  )
    wake.spawnRuns.set(e.data.call_id, wake.run);
  if (e.type === "tool_result_late")
    wake.trailingLate.set(e.event_id, e.data.call_id);
  else if (e.type !== "agent_finished") wake.trailingLate.clear();
}

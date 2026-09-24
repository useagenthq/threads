import { type ParkAddress, sameAddress } from "../fold/state";
import type { KnownEvent } from "../log";

// Run completion (spec/schema/README.md, "Run completion"; Gate 1 decision 27): a run spans its
// request's turn and every wake turn of the same request, and ends at the first point where no
// turn is open, nothing is parked and every background child it spawned has reported. Only the
// lead's own log decides it, so run(), the host's outcome and SSE, and replay agree. Team events
// are refused before the Teams build, so the openers here are user_input and woken.

export type RunStatus =
  | "running"
  | "parked"
  | "completed"
  | "failed"
  | "cancelled"
  | "budget_exhausted"
  | "handed_off";

export type RunEnd = {
  readonly status: RunStatus;
  /** The turn that decided it: the last answered one when completed, else the one that ended
   * otherwise. Empty while running or parked. */
  readonly turn: readonly KnownEvent[];
  /** Where in `events` the run ended; undefined while running or parked. */
  readonly at: number | undefined;
};

/** A turn_completed reason other than end_turn: the status it ends the run with. */
function endedAs(reason: string): RunStatus {
  if (reason === "cancelled" || reason === "budget_exhausted") return reason;
  return reason === "handoff" ? "handed_off" : "failed";
}

class Run {
  readonly #request: string;
  #current: string | undefined;
  #open = false;
  #start = 0;
  readonly #parks: ParkAddress[] = [];
  /** The run that spawned each background call, and each late result's run. */
  readonly #spawned = new Map<string, string>();
  readonly #lateRuns = new Map<string, string>();
  /** This run's background children that have not reported. */
  readonly #children = new Set<string>();
  #answered: readonly KnownEvent[] | undefined;
  decided: RunEnd | undefined;

  constructor(request: string) {
    this.#request = request;
  }

  step(events: readonly KnownEvent[], i: number): void {
    const e = events[i];
    if (e === undefined) return;
    if (!this.#open && (e.type === "user_input" || e.type === "woken")) {
      this.#open = true;
      this.#start = i;
      this.#current =
        e.type === "user_input"
          ? e.event_id
          : this.#lateRuns.get(e.data.causes[0] ?? "");
    }
    this.#helpers(e);
    if (e.type === "turn_completed") this.#turnEnd(events, i, e.data.reason);
    else if (e.type === "parked") this.#parks.push(e.data.address);
    else if (e.type === "resumed") {
      const at = this.#parks.findIndex((p) => sameAddress(p, e.data.address));
      if (at !== -1) this.#parks.splice(at, 1);
    }
    // A late result and the woken naming it are one append: the end is never between them.
    const midAppend =
      e.type === "agent_finished" || e.type === "tool_result_late";
    if (!midAppend && this.decided === undefined && this.ended())
      this.decided = this.#completed(i);
  }

  #helpers(e: KnownEvent): void {
    if (e.type === "agent_spawned" && e.data.mode === "background") {
      if (this.#current === undefined) return;
      this.#spawned.set(e.data.call_id, this.#current);
      if (this.#current === this.#request)
        this.#children.add(e.data.child_thread_id);
    } else if (e.type === "agent_finished")
      this.#children.delete(e.data.child_thread_id);
    else if (e.type === "tool_result_late") {
      const run = this.#spawned.get(e.data.call_id);
      if (run !== undefined) this.#lateRuns.set(e.event_id, run);
    }
  }

  #turnEnd(events: readonly KnownEvent[], i: number, reason: string): void {
    const mine = this.#open && this.#current === this.#request;
    this.#open = false;
    if (!mine || this.decided !== undefined) return;
    const turn = events.slice(this.#start, i + 1);
    if (reason === "end_turn") this.#answered = turn;
    else this.decided = { status: endedAs(reason), turn, at: i };
  }

  /** An answer, no turn open, nothing parked, every child of this run reported. */
  ended(): boolean {
    return (
      this.#answered !== undefined &&
      !this.#open &&
      this.#parks.length === 0 &&
      this.#children.size === 0
    );
  }

  #completed(at: number): RunEnd {
    return { status: "completed", turn: this.#answered ?? [], at };
  }

  /** Where the log leaves a run that has not ended. */
  unfinished(last: number): RunEnd {
    if (this.ended()) return this.#completed(last);
    // A park wins over an open turn: a lead parked mid-turn has returned parked.
    const status = this.#parks.length > 0 ? "parked" : "running";
    return { status, turn: [], at: undefined };
  }
}

/** How the run named by its user_input `request` ended, from the log alone. */
export function runEnd(events: readonly KnownEvent[], request: string): RunEnd {
  const run = new Run(request);
  for (const i of events.keys()) {
    run.step(events, i);
    if (run.decided !== undefined) return run.decided;
  }
  return run.unfinished(events.length - 1);
}

/** The conformance `run` projection: the log's last run. Null for a log with no input. */
export function runProjection(events: readonly KnownEvent[]): {
  readonly status: RunStatus;
  readonly output_event_id: string | null;
} | null {
  const request = events.findLast((e) => e.type === "user_input");
  if (request === undefined) return null;
  const end = runEnd(events, request.event_id);
  const answer = end.turn.findLast(
    (e) => e.type === "model_response" || e.type === "model_response_recovered",
  );
  return {
    status: end.status,
    output_event_id:
      end.status === "completed" ? (answer?.event_id ?? null) : null,
  };
}

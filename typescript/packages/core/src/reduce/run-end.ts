import { type ParkAddress, sameAddress } from "../fold/state";
import { mailRenders } from "../fold/team";
import type { KnownEvent, MailEnvelope } from "../log";

// Run completion (spec/schema/README.md, "Run completion"; Gate 1 decision 27): a run spans its
// request's turn and every wake turn of the same request, and ends at the first point where no
// turn is open, nothing is parked and every background child it spawned has reported. Only the
// lead's own log decides it, so run(), the host's outcome and SSE, and replay agree. A turn opens
// with a user_input, a woken (the run that spawned its children) or a received mail that opens a
// turn (its provenance's root request). Reference: spec/tools/fixtures/run_end.py.

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

/** A run that ended some other way than completed: it is never woken again. */
export function endedOtherwise(status: RunStatus): boolean {
  return (
    status === "failed" ||
    status === "cancelled" ||
    status === "budget_exhausted" ||
    status === "handed_off"
  );
}

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
  /** The task monitors of this run's members that have not reported. */
  readonly #monitors = new Set<string>();
  /** The settle monitors of this log's waits: their notifications open no turn. */
  readonly #settle = new Set<string>();
  #answered: readonly KnownEvent[] | undefined;
  decided: RunEnd | undefined;

  constructor(request: string) {
    this.#request = request;
  }

  step(events: readonly KnownEvent[], i: number): void {
    const e = events[i];
    if (e === undefined) return;
    const opens = this.#open ? undefined : this.#opens(e);
    if (opens !== undefined) {
      this.#open = true;
      this.#start = i;
      this.#current = opens.run;
    }
    this.#helpers(e);
    this.#members(e);
    this.#control(events, i, e);
    // A late result and the woken naming it are one append: the end is never between them.
    const midAppend =
      e.type === "agent_finished" || e.type === "tool_result_late";
    if (!midAppend && this.decided === undefined && this.ended())
      this.decided = this.#completed(i);
  }

  /** The run of the turn `e` opens, or undefined when it opens none. */
  #opens(e: KnownEvent): { readonly run: string | undefined } | undefined {
    if (e.type === "user_input") return { run: e.event_id };
    if (e.type === "woken")
      return { run: this.#lateRuns.get(e.data.causes[0] ?? "") };
    if (e.type !== "message_received" || !this.#mailOpens(e.data.envelope))
      return undefined;
    return { run: e.data.envelope.provenance.root_request.event_id };
  }

  /** A received mail opens a turn once it renders and no park is left but the one it
   * resolves (reference: turn_open.py). */
  #mailOpens(env: MailEnvelope): boolean {
    const resolves = (p: ParkAddress): boolean =>
      p.kind === "member" && p.id === env.monitor_id;
    return mailRenders(env, this.#settle) && this.#parks.every(resolves);
  }

  /** Run-owned members, by their task monitors, and the waits' settle monitors. */
  #members(e: KnownEvent): void {
    if (
      e.type === "member_started" &&
      e.data.provenance.root_request.event_id === this.#request
    )
      this.#monitors.add(`${e.branch_id}:${e.event_id}:task`);
    else if (
      e.type === "message_received" &&
      (e.data.envelope.kind === "member_settled" ||
        e.data.envelope.kind === "member_ended")
    )
      this.#monitors.delete(e.data.envelope.monitor_id ?? "");
    else if (e.type === "wait_started")
      for (const m of e.data.members)
        this.#settle.add(`${e.branch_id}:${e.event_id}:${m.name}`);
  }

  /** Turn ends, parks and idle cancels. */
  #control(events: readonly KnownEvent[], i: number, e: KnownEvent): void {
    if (e.type === "cancel_requested") this.#idleCancel(e.data.scope, i);
    else if (e.type === "turn_completed")
      this.#turnEnd(events, i, e.data.reason);
    else if (e.type === "parked") this.#parks.push(e.data.address);
    else if (e.type === "resumed") {
      const at = this.#parks.findIndex((p) => sameAddress(p, e.data.address));
      if (at !== -1) this.#parks.splice(at, 1);
    }
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

  /** A thread or tree cancel while no turn is open ends a run still waiting on its children. */
  #idleCancel(scope: string, i: number): void {
    const waiting = this.#answered !== undefined && !this.#open;
    if (!waiting || scope === "turn" || this.decided !== undefined) return;
    this.decided = { status: "cancelled", turn: [], at: i };
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
      this.#children.size === 0 &&
      this.#monitors.size === 0
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

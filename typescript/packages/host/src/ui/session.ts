import type { EventId, KnownEvent } from "@threads/core/host";
import type { RunOutcome } from "../outcome";
import { endOf } from "../subscribe";
import type { RunIds } from "./closing";
import { type Cursor, type Step, UiConnection } from "./connection";
import { bare, type Chunk, type Frame, type Protocol } from "./frame";
import type { Delta } from "./live";
import { messagesSnapshot, preamble } from "./snapshot";

// One UI connection's frames over time (spec/schema/ui/README.md): the opening frame, for an
// AG-UI replay the snapshot and open-state preamble at the first read, then the run's events as
// each read finds them, the live deltas in between, and the closing frames. It reads nothing but
// what it is handed, so the host's stream and the conformance runner drive the same code.

export type SessionPlan = {
  readonly protocol: Protocol;
  readonly runId: EventId;
  /** The ids AG-UI's run events echo. */
  readonly ids: RunIds;
  /** Frames the client already has (AI SDK). */
  readonly after?: Cursor;
  /** An AG-UI replay: a snapshot at the first read's head, or through a cursor's event. */
  readonly replay?: "head" | number;
  /** Envelope frames after the opening (and the snapshot): resume conflicts. */
  readonly extra?: readonly Chunk[];
  /** The ui receipts' message ids by run id, for a snapshot's user messages. */
  readonly receipts?: ReadonlyMap<string, string>;
};

export class UiSession {
  readonly #plan: SessionPlan;
  readonly #connection: UiConnection;
  #seen = 0;

  /**
   * Opens on the log as the first read after registration finds it. `missed`: the live hub's
   * started parts at registration; absent, the connection streams no live text.
   */
  constructor(
    plan: SessionPlan,
    events: readonly KnownEvent[],
    missed?: ReadonlySet<string>,
  ) {
    this.#plan = plan;
    const own = runEvents(events, plan.runId);
    const h =
      plan.replay === "head" ? (own.at(-1)?.seq ?? 0) : (plan.replay ?? 0);
    // A replay's events through its snapshot point are folded, never sent.
    const after =
      plan.replay === undefined
        ? plan.after
        : { seq: h, k: Number.POSITIVE_INFINITY };
    this.#connection = new UiConnection({
      protocol: plan.protocol,
      ...(after === undefined ? {} : { after }),
      ...(missed === undefined ? {} : { missed }),
    });
    if (plan.replay === undefined) return;
    for (const e of own) if (e.seq <= h) this.#connection.event(e);
    this.#seen = h;
  }

  /** The opening frames; for a replay, the snapshot of `events` through its point. */
  opening(events: readonly KnownEvent[]): readonly Frame[] {
    const plan = this.#plan;
    const start: Chunk =
      plan.protocol === "ai-sdk"
        ? { type: "start", messageId: plan.runId }
        : { type: "RUN_STARTED", ...plan.ids };
    if (plan.replay === undefined) return bare([start, ...(plan.extra ?? [])]);
    const history = events.filter((e) => e.seq <= this.#seen);
    return bare([
      start,
      messagesSnapshot(history, plan.receipts ?? new Map()),
      ...(plan.extra ?? []),
      ...preamble(this.#connection.facts),
    ]);
  }

  deltas(deltas: readonly Delta[]): readonly Frame[] {
    return deltas.flatMap((d) => this.#connection.delta(d));
  }

  /** The frames of the run's events this read finds; `broken` ends the connection. */
  read(events: readonly KnownEvent[]): Step {
    const frames: Frame[] = [];
    for (const e of runEvents(events, this.#plan.runId)) {
      if (e.seq <= this.#seen) continue;
      this.#seen = e.seq;
      const step = this.#connection.event(e);
      frames.push(...step.frames);
      if (step.broken !== undefined) return { frames, broken: step.broken };
    }
    return { frames };
  }

  close(outcome: RunOutcome): readonly Frame[] {
    return this.#connection.close(outcome, this.#plan.ids);
  }
}

/** The run's own events: from its user_input through its end, or the latest while it goes on. */
export function runEvents(
  events: readonly KnownEvent[],
  runId: EventId,
): readonly KnownEvent[] {
  const start = events.findIndex((e) => e.event_id === runId);
  if (start === -1) return [];
  return events.slice(start, start + endOf(events, runId, start) + 1);
}

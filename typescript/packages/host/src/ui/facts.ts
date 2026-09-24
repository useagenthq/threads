import {
  assertNever,
  type EventOf,
  type JsonObject,
  type KnownEvent,
  type ParkAddress,
} from "@threads/core/host";

// What a run's frames read from the events before one: the purpose of each model request,
// whether a model step is open, the legacy subagents it started, its tool calls, approval
// challenges and park reasons. Fed one committed event at a time, in log order.

export type Spawned = {
  readonly child: string;
  readonly agent: string;
  readonly callId: string;
};

export type Challenge = { readonly callId: string; readonly expiresAt: number };

type FactEvent = EventOf<
  | "model_request"
  | "model_response"
  | "model_response_recovered"
  | "model_attempt_abandoned"
  | "agent_spawned"
  | "agent_finished"
  | "tool_call"
  | "approval_requested"
  | "parked"
>;
const FACT_TYPES = {
  model_request: true,
  model_response: true,
  model_response_recovered: true,
  model_attempt_abandoned: true,
  agent_spawned: true,
  agent_finished: true,
  tool_call: true,
  approval_requested: true,
  parked: true,
} satisfies Record<FactEvent["type"], true>;

function isFact(e: KnownEvent): e is FactEvent {
  return Object.hasOwn(FACT_TYPES, e.type);
}

export class RunFacts {
  readonly #purposes = new Map<string, "turn" | "compaction">();
  readonly #answered = new Set<string>();
  /** Calls a turn response of this run proposed: a result shows only for one of these. */
  readonly #shown = new Set<string>();
  #lastTurn: string | undefined;
  #lastStop: string | undefined;
  readonly #spawned = new Map<string, Spawned>();
  readonly #finished = new Set<string>();
  readonly #calls = new Map<
    string,
    { readonly name: string; readonly input: JsonObject }
  >();
  readonly #challenges = new Map<string, Challenge>();
  readonly #parks = new Map<string, string>();

  add(e: KnownEvent): void {
    if (!isFact(e)) return;
    switch (e.type) {
      case "model_request":
        this.#purposes.set(e.event_id, e.data.purpose ?? "turn");
        if (e.data.purpose !== "compaction") this.#lastTurn = e.event_id;
        return;
      case "model_response":
      case "model_response_recovered":
        this.#answered.add(e.data.request_event_id);
        if (!this.isTurn(e.data.request_event_id)) return;
        this.#lastStop = e.data.stop_reason;
        for (const part of e.data.content)
          if (part.type === "tool_use") this.#shown.add(part.call_id);
        return;
      case "model_attempt_abandoned":
        this.#answered.add(e.data.request_event_id);
        return;
      case "agent_spawned":
        this.#spawned.set(e.data.child_thread_id, {
          child: e.data.child_thread_id,
          agent: e.data.agent_name,
          callId: e.data.call_id,
        });
        return;
      case "agent_finished":
        this.#finished.add(e.data.child_thread_id);
        return;
      case "tool_call":
        this.#calls.set(e.data.call_id, {
          name: e.data.name,
          input: e.data.input,
        });
        return;
      case "approval_requested":
        this.#challenges.set(e.data.challenge_id, {
          callId: e.data.call_id,
          expiresAt: e.data.expires_at,
        });
        return;
      case "parked":
        this.#parks.set(key(e.data.address), e.data.reason);
        return;
      default:
        assertNever(e);
    }
  }

  /** A compaction side request's frames are none; an unknown request counts as a turn's. */
  isTurn(requestId: string): boolean {
    return this.#purposes.get(requestId) !== "compaction";
  }

  /** The turn request with no response and no abandonment yet, if the last one is open. */
  openStep(): string | undefined {
    const last = this.#lastTurn;
    return last !== undefined && !this.#answered.has(last) ? last : undefined;
  }

  /** The stop reason of the run's latest turn response. */
  lastStop(): string | undefined {
    return this.#lastStop;
  }

  /** Whether a turn response of this run proposed the call, so the client holds its part. */
  proposed(callId: string): boolean {
    return this.#shown.has(callId);
  }

  spawned(child: string): Spawned | undefined {
    return this.#spawned.get(child);
  }

  /** Legacy subagents started in this run and not finished, in start order. */
  running(): readonly Spawned[] {
    return [...this.#spawned.values()].filter(
      (s) => !this.#finished.has(s.child),
    );
  }

  call(
    callId: string,
  ): { readonly name: string; readonly input: JsonObject } | undefined {
    return this.#calls.get(callId);
  }

  challenge(id: string): Challenge | undefined {
    return this.#challenges.get(id);
  }

  parkReason(address: ParkAddress): string | undefined {
    return this.#parks.get(key(address));
  }
}

const key = (a: ParkAddress): string => `${a.kind}:${a.id}`;

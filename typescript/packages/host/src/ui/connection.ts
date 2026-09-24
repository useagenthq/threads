import type { KnownEvent } from "@threads/core/host";
import type { RunOutcome } from "../outcome";
import { agUiEvents } from "./ag-ui";
import { aiSdkChunks } from "./ai-sdk";
import { closing, type RunIds, textEnd } from "./closing";
import { RunFacts } from "./facts";
import { bare, type Frame, type Protocol } from "./frame";
import { partId, shownParts } from "./framed";
import { type Delta, LiveParts } from "./live";

// One UI connection's frames (spec/schema/ui/README.md): the run's committed events in log
// order, each as its frames with ids <seq>:<k>, the frames at or before a cursor left out, live
// text for the parts this connection may stream, and the closing frames. It reads nothing but
// what it is given, so a replay of the log gives the same frames.

/** A frame position: frames of event `seq` up to index `k` (Infinity: all of them). */
export type Cursor = { readonly seq: number; readonly k: number };

export type ConnectionOptions = {
  readonly protocol: Protocol;
  /** Frames at or before this position are not sent: the client has them. */
  readonly after?: Cursor;
  /** With live text: the parts the hub had seen deltas for when this connection registered. */
  readonly missed?: ReadonlySet<string>;
};

/** A step's frames; `broken` names a live part whose commit contradicts it: close, no [DONE]. */
export type Step = {
  readonly frames: readonly Frame[];
  readonly broken?: string;
};

export class UiConnection {
  readonly facts: RunFacts = new RunFacts();
  readonly #protocol: Protocol;
  readonly #after: Cursor | undefined;
  readonly #live: LiveParts | undefined;
  /** Turn requests whose model_request this connection has passed. */
  readonly #requests = new Set<string>();
  /** Requests committed or abandoned: their late deltas are dropped. */
  readonly #settled = new Set<string>();
  /** Deltas of a request whose model_request this connection hasn't read yet. */
  readonly #queued = new Map<string, Delta[]>();

  constructor(options: ConnectionOptions) {
    this.#protocol = options.protocol;
    this.#after = options.after;
    this.#live =
      options.missed === undefined
        ? undefined
        : new LiveParts(options.protocol, options.missed);
  }

  /** The frames of the next committed event of the run. */
  event(e: KnownEvent): Step {
    const all = eventFrames(this.#protocol, e, this.facts);
    const frames = all.filter((_, k) => this.#sends(e.seq, k));
    const live = this.#live;
    if (live === undefined) return { frames };
    if (e.type === "model_request") {
      // A compaction side request's text never shows, live or committed.
      if (e.data.purpose === "compaction") {
        this.#settled.add(e.event_id);
        return { frames };
      }
      this.#requests.add(e.event_id);
      const queued = this.#queued.get(e.event_id) ?? [];
      this.#queued.delete(e.event_id);
      return { frames: [...frames, ...queued.flatMap((d) => live.delta(d))] };
    }
    if (e.type === "model_attempt_abandoned") {
      const requestId = e.data.request_event_id;
      this.#settled.add(requestId);
      const open = live.settle(requestId);
      return {
        frames: [
          ...open.map((id) => ({ data: textEnd(this.#protocol, id) })),
          ...frames,
        ],
      };
    }
    if (e.type === "model_response" || e.type === "model_response_recovered") {
      const requestId = e.data.request_event_id;
      this.#settled.add(requestId);
      const texts = new Map(
        shownParts(e).flatMap((p) =>
          p.kind === "text" ? [[partId(requestId, p.index), p.text]] : [],
        ),
      );
      const open = live.open().filter((id) => id.startsWith(`${requestId}:`));
      const out = live.substitute(requestId, frames, texts);
      if ("mismatch" in out)
        return {
          frames: open.map((id) => ({ data: textEnd(this.#protocol, id) })),
          broken: out.mismatch,
        };
      return { frames: out };
    }
    return { frames };
  }

  /** The frames one live delta adds on this connection, if any. */
  delta(d: Delta): readonly Frame[] {
    if (this.#live === undefined || this.#settled.has(d.requestId)) return [];
    if (this.#requests.has(d.requestId)) return this.#live.delta(d);
    this.#queued.set(d.requestId, [
      ...(this.#queued.get(d.requestId) ?? []),
      d,
    ]);
    return [];
  }

  /** The closing frames once the run has ended (or parked). */
  close(outcome: RunOutcome, ids: RunIds): readonly Frame[] {
    return bare(
      closing(
        this.#protocol,
        outcome,
        this.facts,
        this.#live?.open() ?? [],
        ids,
      ),
    );
  }

  #sends(seq: number, k: number): boolean {
    const after = this.#after;
    return (
      after === undefined ||
      seq > after.seq ||
      (seq === after.seq && k > after.k)
    );
  }
}

/** A committed event's canonical frames, given the run's events before it; then it is one of them. */
export function eventFrames(
  protocol: Protocol,
  e: KnownEvent,
  facts: RunFacts,
): readonly Frame[] {
  const chunks =
    protocol === "ai-sdk" ? aiSdkChunks(e, facts) : agUiEvents(e, facts);
  facts.add(e);
  return chunks.map((data, k) => ({ id: `${e.seq}:${k}`, data }));
}

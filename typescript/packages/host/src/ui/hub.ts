import type { KnownEvent } from "@threads/core/host";
import { partId } from "./framed";
import type { Delta } from "./live";

// The process's live hub (spec/schema/ui/README.md, "Live text"): the runs this process executes
// publish their redacted text deltas here, keyed by thread, and the UI connections on that
// thread listen. Per (request, part) it remembers that a delta went out, until the request
// commits or is abandoned, so a connection registering mid-part never takes a later delta for
// the first.

export type HubItem =
  | { readonly kind: "delta"; readonly delta: Delta }
  | { readonly kind: "appended" };

type Listener = (item: HubItem) => void;

type Channel = {
  readonly started: Set<string>;
  readonly listeners: Set<Listener>;
};

export type Registration = {
  /** Parts a delta had already gone out for: this connection never streams them live. */
  readonly missed: ReadonlySet<string>;
  readonly stop: () => void;
};

export class LiveHub {
  readonly #threads = new Map<string, Channel>();

  #channel(thread: string): Channel {
    const found = this.#threads.get(thread);
    if (found !== undefined) return found;
    const made: Channel = { started: new Set(), listeners: new Set() };
    this.#threads.set(thread, made);
    return made;
  }

  #drop(thread: string, c: Channel): void {
    if (c.started.size === 0 && c.listeners.size === 0)
      this.#threads.delete(thread);
  }

  /** A redacted delta of a run on `thread`. */
  delta(thread: string, delta: Delta): void {
    const c = this.#channel(thread);
    c.started.add(partId(delta.requestId, delta.part));
    for (const listen of c.listeners) listen({ kind: "delta", delta });
  }

  /** An event a run of this process appended on `thread`: listeners re-read the log. */
  appended(thread: string, e: KnownEvent): void {
    const c = this.#threads.get(thread);
    if (c === undefined) return;
    if (
      e.type === "model_response" ||
      e.type === "model_response_recovered" ||
      e.type === "model_attempt_abandoned"
    )
      for (const id of c.started)
        if (id.startsWith(`${e.data.request_event_id}:`)) c.started.delete(id);
    for (const listen of c.listeners) listen({ kind: "appended" });
    this.#drop(thread, c);
  }

  /** Registers before the connection reads the head, so no delta falls between the two. */
  listen(thread: string, listener: Listener): Registration {
    const c = this.#channel(thread);
    c.listeners.add(listener);
    return {
      missed: new Set(c.started),
      stop: () => {
        c.listeners.delete(listener);
        this.#drop(thread, c);
      },
    };
  }
}

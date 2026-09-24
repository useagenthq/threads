import type { KnownEvent } from "@threads/core/host";
import { partId } from "./framed";
import type { Delta } from "./live";

// The process's live hub (spec/schema/ui/README.md, "Live text"): the runs this process executes
// publish their redacted text deltas here, keyed by tenant and thread, and the UI connections on
// that thread listen. The tenant is part of the key, so a thread id alone never reaches another
// tenant's text. Per (request, part) it remembers that a delta went out, until the request
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

  #channel(key: string): Channel {
    const found = this.#threads.get(key);
    if (found !== undefined) return found;
    const made: Channel = { started: new Set(), listeners: new Set() };
    this.#threads.set(key, made);
    return made;
  }

  #drop(key: string, c: Channel): void {
    if (c.started.size === 0 && c.listeners.size === 0)
      this.#threads.delete(key);
  }

  /** A redacted delta of a run on `thread`. */
  delta(tenant: string, thread: string, delta: Delta): void {
    const c = this.#channel(keyOf(tenant, thread));
    c.started.add(partId(delta.requestId, delta.part));
    for (const listen of c.listeners) listen({ kind: "delta", delta });
  }

  /** An event a run of this process appended on `thread`: listeners re-read the log. */
  appended(tenant: string, e: KnownEvent): void {
    const key = keyOf(tenant, e.thread_id);
    const c = this.#threads.get(key);
    if (c === undefined) return;
    if (
      e.type === "model_response" ||
      e.type === "model_response_recovered" ||
      e.type === "model_attempt_abandoned"
    )
      for (const id of c.started)
        if (id.startsWith(`${e.data.request_event_id}:`)) c.started.delete(id);
    for (const listen of c.listeners) listen({ kind: "appended" });
    this.#drop(key, c);
  }

  /** Registers before the connection reads the head, so no delta falls between the two. */
  listen(tenant: string, thread: string, listener: Listener): Registration {
    const key = keyOf(tenant, thread);
    const c = this.#channel(key);
    c.listeners.add(listener);
    return {
      missed: new Set(c.started),
      stop: () => {
        c.listeners.delete(listener);
        this.#drop(key, c);
      },
    };
  }
}

function keyOf(tenant: string, thread: string): string {
  return JSON.stringify([tenant, thread]);
}

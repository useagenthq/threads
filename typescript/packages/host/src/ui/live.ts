import type { Chunk, Frame, Protocol } from "./frame";
import { partId } from "./framed";

// Live text on one connection (spec/schema/ui/README.md, "Live text"): which parts it streams
// as deltas arrive, and how a part's committed frames are substituted once its response lands.
// Deltas are best-effort and never logged; every frame this sends carries no SSE id.

export type Delta = {
  readonly requestId: string;
  readonly part: number;
  readonly text: string;
};

/** A live part whose commit contradicts what was streamed: the connection must close. */
export type Mismatch = { readonly mismatch: string };

export class LiveParts {
  readonly #protocol: Protocol;
  /** Parts the hub had seen a delta for when this connection registered: never attached. */
  readonly #missed: ReadonlySet<string>;
  /** Text sent live per open part id, in the order the parts opened. */
  readonly #sent = new Map<string, string>();
  /** Parts whose first delta this connection saw and did not attach. */
  readonly #refused = new Set<string>();
  /** Requests whose live streaming stopped on this connection (the order rule). */
  readonly #stopped = new Set<string>();

  constructor(protocol: Protocol, missed: ReadonlySet<string>) {
    this.#protocol = protocol;
    this.#missed = missed;
  }

  /** The frames one delta adds: a part opens only at its first delta, in model order. */
  delta(d: Delta): readonly Frame[] {
    const id = partId(d.requestId, d.part);
    const sent = this.#sent.get(id);
    if (sent !== undefined) {
      this.#sent.set(id, sent + d.text);
      return [{ data: this.#text(id, d.text) }];
    }
    if (this.#missed.has(id) || this.#refused.has(id)) return [];
    if (!this.#attaches(d)) {
      this.#refused.add(id);
      this.#stopped.add(d.requestId);
      return [];
    }
    this.#sent.set(id, d.text);
    return [{ data: this.#start(id) }, { data: this.#text(id, d.text) }];
  }

  /** Part i streams only while every part before it streamed here, so parts keep model order. */
  #attaches(d: Delta): boolean {
    if (this.#stopped.has(d.requestId)) return false;
    return d.part === 0 || this.#sent.has(partId(d.requestId, d.part - 1));
  }

  /** The text sent live for a part, if this connection streams it. */
  sent(id: string): string | undefined {
    return this.#sent.get(id);
  }

  /** Closes a request's live parts: at its commit (their frames follow) or its abandonment. */
  settle(requestId: string): readonly string[] {
    const open = [...this.#sent.keys()].filter((id) =>
      id.startsWith(`${requestId}:`),
    );
    for (const id of open) this.#sent.delete(id);
    this.#stopped.delete(requestId);
    return open;
  }

  /** Every part still open live, in the order they opened. */
  open(): readonly string[] {
    return [...this.#sent.keys()];
  }

  /**
   * The canonical frames of a committed response with this connection's live parts
   * substituted: a live part skips its start and gets only the rest of its text.
   */
  substitute(
    requestId: string,
    frames: readonly Frame[],
    textAt: ReadonlyMap<string, string>,
  ): readonly Frame[] | Mismatch {
    for (const [id, sent] of this.#sent) {
      if (!id.startsWith(`${requestId}:`)) continue;
      const committed = textAt.get(id);
      if (committed === undefined || !committed.startsWith(sent))
        return { mismatch: id };
    }
    const out = frames.flatMap((f): Frame[] => {
      const id = this.#partOf(f.data);
      const sent = id === undefined ? undefined : this.#sent.get(id);
      if (id === undefined || sent === undefined) return [f];
      if (this.#isStart(f.data)) return [];
      if (!this.#isContent(f.data)) return [f];
      const rest = (textAt.get(id) ?? "").slice(sent.length);
      return rest === "" ? [] : [{ ...f, data: this.#text(id, rest) }];
    });
    this.settle(requestId);
    return out;
  }

  #start(id: string): Chunk {
    return this.#protocol === "ai-sdk"
      ? { type: "text-start", id }
      : { type: "TEXT_MESSAGE_START", messageId: id, role: "assistant" };
  }

  #text(id: string, delta: string): Chunk {
    return this.#protocol === "ai-sdk"
      ? { type: "text-delta", id, delta }
      : { type: "TEXT_MESSAGE_CONTENT", messageId: id, delta };
  }

  /** The part a text start or content frame belongs to; undefined for any other frame. */
  #partOf(data: Chunk): string | undefined {
    if (!this.#isStart(data) && !this.#isContent(data)) return undefined;
    const id = data[this.#protocol === "ai-sdk" ? "id" : "messageId"];
    return typeof id === "string" ? id : undefined;
  }

  #isStart(data: Chunk): boolean {
    return data.type === "text-start" || data.type === "TEXT_MESSAGE_START";
  }

  #isContent(data: Chunk): boolean {
    return data.type === "text-delta" || data.type === "TEXT_MESSAGE_CONTENT";
  }
}

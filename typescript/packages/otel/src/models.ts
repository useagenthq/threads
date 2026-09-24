import type { EventOf, KnownEvent } from "@threads/core/internal/feed";
import { chatAttrs, responseAttrs, spanEvent } from "./attrs";
import { spanId } from "./ids";
import { CLIENT, OpenSpan, type Span } from "./span";
import type { Turn } from "./turns";

// Model call spans (chat) and where a retry_scheduled goes: the next attempt of its turn that
// is not a compaction side request, else the turn (spec/otel/README.md, "Span events").

type ModelRef = EventOf<"thread_started">["data"]["model"];

export class Models {
  readonly #open = new Map<string, OpenSpan>();
  /** Every request's span id, so a retry names the attempt that failed. */
  readonly #ids = new Map<string, string>();
  #retry: EventOf<"retry_scheduled"> | undefined;
  model: ModelRef = { provider: "unknown", name: "unknown" };

  constructor(readonly content: boolean) {}

  request(e: EventOf<"model_request">, turn: Turn): void {
    const id = spanId(e.branch_id, e.event_id);
    this.#ids.set(e.event_id, id);
    const span = new OpenSpan(
      { traceId: turn.context.traceId, spanId: id },
      turn.span.id.spanId,
      `chat ${this.model.name}`,
      CLIENT,
      e,
      chatAttrs(this.model, e),
    );
    const retry = this.#retry;
    if (retry !== undefined && e.data.purpose !== "compaction") {
      span.events.push(spanEvent(retry, this.#failed(retry)));
      this.#retry = undefined;
    }
    this.#open.set(e.event_id, span);
  }

  /** A response, recovered response or abandonment closes its request's span. */
  close(
    e:
      | EventOf<"model_response">
      | EventOf<"model_response_recovered">
      | EventOf<"model_attempt_abandoned">,
    branchId: string,
    tenant: string,
  ): Span | undefined {
    const span = this.#open.get(e.data.request_event_id);
    if (span === undefined) return undefined;
    this.#open.delete(e.data.request_event_id);
    if (e.type === "model_attempt_abandoned") {
      span.attrs["error.type"] = e.data.reason;
      return span.close(e, branchId, tenant, e.data.reason);
    }
    Object.assign(span.attrs, responseAttrs(e, this.content));
    return span.close(e, branchId, tenant, undefined);
  }

  /** Held for the next attempt; a turn that closes first takes it. */
  retry(e: EventOf<"retry_scheduled">): void {
    this.#retry = e;
  }

  /** A turn's close: model spans still open are cut, and a held retry goes to the turn. */
  endAll(e: KnownEvent, turn: Turn, branchId: string, tenant: string): Span[] {
    const retry = this.#retry;
    if (retry !== undefined)
      turn.span.events.push(spanEvent(retry, this.#failed(retry)));
    this.#retry = undefined;
    const cut = [...this.#open.values()].map((span) => {
      span.attrs["threads.cut"] = true;
      return span.close(e, branchId, tenant, "cut");
    });
    this.#open.clear();
    return cut;
  }

  #failed(retry: EventOf<"retry_scheduled">): string | undefined {
    return this.#ids.get(retry.data.request_event_id);
  }
}

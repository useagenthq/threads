import type { EventOf, KnownEvent } from "@threads/core/internal/feed";
import { spanEvent, toolAttrs } from "./attrs";
import { spanId } from "./ids";
import { INTERNAL, type Link, OpenSpan, type Span } from "./span";
import type { Turn } from "./turns";

// Tool spans and their continuations (spec/otel/README.md): one open span per call at most, the
// calls still pending, and which of them can run when a parked turn resumes.

type ToolCall = EventOf<"tool_call">;

export class Calls {
  /** The open tool or continuation span of each call. */
  readonly open: Map<string, OpenSpan> = new Map();
  /** Calls with a tool_call and no tool_result yet, in call order. */
  readonly #pending = new Map<string, ToolCall>();
  /** Calls asked about (permission ask, approval requested) and not answered yet. */
  readonly #awaiting = new Set<string>();
  readonly #denied = new Set<string>();
  /** Each call's latest span, which its next continuation links to. */
  readonly #previous = new Map<string, Link>();
  /** Effect classes of the latest pinned tool set, by tool name. */
  effectClasses: ReadonlyMap<string, string> = new Map();

  constructor(readonly content: boolean) {}

  /** Tracks what decides which calls a resumed turn continues. */
  see(e: KnownEvent): void {
    if (e.type === "tool_call") this.#pending.set(e.data.call_id, e);
    else if (e.type === "tool_result") this.#pending.delete(e.data.call_id);
    else if (e.type === "permission_decision" && e.data.decision === "ask")
      this.#awaiting.add(e.data.call_id);
    else if (e.type === "approval_requested")
      this.#awaiting.add(e.data.call_id);
    else if (e.type === "approval_granted")
      this.#awaiting.delete(e.data.call_id);
    else if (e.type === "approval_denied") {
      this.#awaiting.delete(e.data.call_id);
      this.#denied.add(e.data.call_id);
    }
  }

  /** The tool span a tool_call opens under its turn. */
  call(e: ToolCall, turn: Turn): void {
    this.#start(e, e, turn, spanId(e.branch_id, e.event_id), false);
  }

  /** At a resumed: a continuation of every pending call that can run now. */
  resume(e: EventOf<"resumed">, turn: Turn): void {
    for (const [callId, call] of this.#pending) {
      const waiting = this.#awaiting.has(callId) || this.#denied.has(callId);
      if (waiting || this.open.has(callId)) continue;
      const id = spanId(e.branch_id, e.event_id, callId);
      const previous = this.#previous.get(callId);
      this.#start(
        e,
        call,
        turn,
        id,
        true,
        previous === undefined ? [] : [previous],
      );
    }
  }

  /** Attaches a call's event to its open span; false when it has none. */
  note(callId: string, e: KnownEvent): boolean {
    const span = this.open.get(callId);
    span?.events.push(spanEvent(e));
    return span !== undefined;
  }

  /** The call's result closes its span, with status ERROR for an error result. */
  result(
    e: EventOf<"tool_result">,
    branchId: string,
    tenant: string,
  ): Span | undefined {
    const span = this.open.get(e.data.call_id);
    if (span === undefined) return undefined;
    this.open.delete(e.data.call_id);
    if (this.content) span.attrs["gen_ai.tool.call.result"] = e.data.preview;
    return span.close(
      e,
      branchId,
      tenant,
      e.data.is_error ? e.data.origin : undefined,
    );
  }

  /** A turn's close closes every call span still open: parked by a park, else cut. */
  endAll(e: KnownEvent, branchId: string, tenant: string): Span[] {
    const closed = [...this.open.values()].map((span) => {
      if (e.type === "parked") {
        span.attrs["threads.parked"] = true;
        return span.close(e, branchId, tenant, undefined);
      }
      span.attrs["threads.cut"] = true;
      return span.close(e, branchId, tenant, "cut");
    });
    this.open.clear();
    return closed;
  }

  #start(
    opener: KnownEvent,
    call: ToolCall,
    turn: Turn,
    id: string,
    resumed: boolean,
    links: readonly Link[] = [],
  ): void {
    const attrs = toolAttrs(
      call,
      this.effectClasses.get(call.data.name),
      resumed,
      this.content,
    );
    const link = { traceId: turn.context.traceId, spanId: id };
    const span = new OpenSpan(
      link,
      turn.span.id.spanId,
      `execute_tool ${call.data.name}`,
      INTERNAL,
      opener,
      attrs,
      links,
    );
    this.open.set(call.data.call_id, span);
    this.#previous.set(call.data.call_id, link);
  }
}

import type { EventOf, KnownEvent } from "@threads/core/internal/feed";
import { spanEvent, turnAttrs, turnEnd } from "./attrs";
import { spanId, traceId } from "./ids";
import { type Context, INTERNAL, type Link, OpenSpan, type Span } from "./span";

// Turn spans: which trace a turn joins (spec/otel/README.md, "Roots and parents"), and the
// bookkeeping of the turn that is open, the one a park closed, and the runs they belong to.

/** Where a child thread's first turn goes, as the parent chain says; undefined: missing. */
export type ParentOf = (
  parent: NonNullable<EventOf<"thread_started">["data"]["parent"]>,
) => Link | undefined;

type TurnContext = Context & { readonly parentMissing: boolean };

/** A turn span and the context its resumed turns inherit. */
export type Turn = { readonly span: OpenSpan; readonly context: TurnContext };

export class Turns {
  open: Turn | undefined;
  /** The turn a park closed, until the next event opens the resumed turn. */
  parked: Turn | undefined;
  /** Each call's turn context, for a woken turn that its late result opens. */
  readonly callContexts: Map<string, TurnContext> = new Map();
  #lateCalls = new Map<string, string>();
  #lastInput: string | undefined;
  #started: EventOf<"thread_started"> | undefined;
  #turns = 0;

  constructor(
    readonly parentOf: ParentOf,
    readonly agent: () => string,
  ) {}

  /** Bookkeeping every event feeds, before any span opens at it. */
  see(e: KnownEvent): void {
    if (e.type === "thread_started") this.#started = e;
    if (e.type === "user_input") this.#lastInput = e.event_id;
    if (e.type === "tool_result_late")
      this.#lateCalls.set(e.event_id, e.data.call_id);
  }

  /** A turn opener's turn, in the trace its rules give. */
  begin(e: KnownEvent): Turn {
    const context = this.#first() ?? this.#rootOf(e);
    this.#turns += 1;
    return this.#start(e, context, []);
  }

  /** The turn a park interrupted goes on at `e`, in its trace, linked to its span. */
  resume(e: KnownEvent, parked: Turn): Turn {
    return this.#start(e, parked.context, [parked.span.id]);
  }

  /** Closes the open turn at a park or a turn_completed. */
  end(e: KnownEvent, branchId: string, tenant: string): Span | undefined {
    const turn = this.open;
    if (turn === undefined) return undefined;
    this.open = undefined;
    if (e.type === "turn_completed") {
      const { attrs, status } = turnEnd(e.data.reason);
      Object.assign(turn.span.attrs, attrs);
      return turn.span.close(e, branchId, tenant, status);
    }
    turn.span.attrs["threads.parked"] = true;
    this.parked = turn;
    return turn.span.close(e, branchId, tenant, undefined);
  }

  /** Attaches a span event to the open turn, if there is one. */
  note(e: KnownEvent, retryOf?: string): void {
    this.open?.span.events.push(spanEvent(e, retryOf));
  }

  #start(e: KnownEvent, context: TurnContext, links: readonly Link[]): Turn {
    const id = {
      traceId: context.traceId,
      spanId: spanId(e.branch_id, e.event_id),
    };
    const attrs = turnAttrs(
      this.agent(),
      e.thread_id,
      this.#lastInput,
      context.parentMissing,
    );
    const span = new OpenSpan(
      id,
      context.parentSpanId,
      `invoke_agent ${this.agent()}`,
      INTERNAL,
      e,
      attrs,
      links,
    );
    this.open = { span, context };
    this.parked = undefined;
    return this.open;
  }

  /** A child thread's first turn: under the parent's span, or its own root if that is gone. */
  #first(): TurnContext | undefined {
    const parent = this.#started?.data.parent;
    if (this.#turns > 0 || parent === undefined) return undefined;
    if (parent.relation === "team_member") return undefined;
    const anchor = this.parentOf(parent);
    if (anchor === undefined) return undefined;
    return {
      traceId: anchor.traceId,
      parentSpanId: anchor.spanId,
      parentMissing: false,
    };
  }

  #rootOf(e: KnownEvent): TurnContext {
    const missing =
      this.#turns === 0 &&
      this.#started?.data.parent !== undefined &&
      this.#started.data.parent.relation !== "team_member";
    const own = {
      traceId: traceId(e.thread_id, e.event_id),
      parentSpanId: undefined,
      parentMissing: missing,
    };
    if (e.type === "woken") {
      const cause = e.data.causes[0];
      const call = cause === undefined ? undefined : this.#lateCalls.get(cause);
      const joined =
        call === undefined ? undefined : this.callContexts.get(call);
      return joined === undefined
        ? own
        : {
            traceId: joined.traceId,
            parentSpanId: undefined,
            parentMissing: missing,
          };
    }
    if (e.type === "message_received") {
      const root = e.data.envelope.provenance.root_request;
      return {
        traceId: traceId(root.thread_id, root.event_id),
        parentSpanId: undefined,
        parentMissing: missing,
      };
    }
    return own;
  }
}

import type { KnownEvent } from "@threads/core/internal/feed";

// A span while the walk builds it, and the closed span it becomes (spec/otel/README.md).

export type AttrValue = string | number | boolean | readonly string[];
export type Attrs = Record<string, AttrValue | undefined>;

export const INTERNAL = 1;
export const CLIENT = 3;

export type SpanEvent = {
  readonly time: number;
  readonly name: string;
  readonly attributes: Attrs;
};

export type Link = { readonly traceId: string; readonly spanId: string };

/** A closed span: what spans() returns and the encoder writes. Times are ms. */
export type Span = Link & {
  readonly branchId: string;
  readonly closeSeq: number;
  readonly parentSpanId: string | undefined;
  readonly name: string;
  readonly kind: typeof INTERNAL | typeof CLIENT;
  readonly start: number;
  readonly end: number;
  readonly attributes: Attrs;
  readonly events: readonly SpanEvent[];
  readonly links: readonly Link[];
  readonly status: string | undefined;
};

/** Where a span sits: its trace, and the span it is a child of. */
export type Context = {
  readonly traceId: string;
  readonly parentSpanId: string | undefined;
};

/** A span the walk has opened and not yet closed. */
export class OpenSpan {
  readonly events: SpanEvent[] = [];
  readonly attrs: Attrs;

  constructor(
    readonly id: Link,
    readonly parentSpanId: string | undefined,
    readonly name: string,
    readonly kind: typeof INTERNAL | typeof CLIENT,
    readonly opener: KnownEvent,
    attrs: Attrs,
    readonly links: readonly Link[] = [],
  ) {
    this.attrs = { ...attrs };
  }

  /** The span as `close` ends it, for the branch `branchId` exports. */
  close(
    close: KnownEvent,
    branchId: string,
    tenant: string,
    status: string | undefined,
  ): Span {
    const skew = close.time < this.opener.time;
    return {
      ...this.id,
      branchId,
      closeSeq: close.seq,
      parentSpanId: this.parentSpanId,
      name: this.name,
      kind: this.kind,
      start: this.opener.time,
      end: skew ? this.opener.time : close.time,
      attributes: {
        ...this.attrs,
        "threads.tenant": tenant,
        "threads.thread_id": this.opener.thread_id,
        "threads.branch_id": branchId,
        "threads.seq.start": this.opener.seq,
        "threads.seq.end": close.seq,
        "threads.clock_skew": skew || undefined,
      },
      events: this.events,
      links: this.links,
      status,
    };
  }
}

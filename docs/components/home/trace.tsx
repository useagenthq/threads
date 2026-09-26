/*
 * The same run as the timeline above, seen as OpenTelemetry spans.
 *
 * Span names, kinds and attribute keys are the ones the exporter actually sends
 * (spec/otel/README.md and the Observability guide). We draw no vendor UI: the point is that the
 * spans are ordinary OTLP, so they land in whatever tracing tool the reader already runs.
 */

type Span = {
  /** The span id the exporter derives from the opening event; here, its first bytes. */
  readonly id: string;
  readonly op: string;
  readonly arg: string;
  readonly kind: "INTERNAL" | "CLIENT";
  readonly took: string;
  readonly attrs: readonly string[];
  readonly events?: readonly string[];
  readonly children?: readonly Span[];
  readonly note?: string;
};

const TRACE: readonly Span[] = [
  {
    id: "c41d8a7e",
    op: "invoke_agent",
    arg: "support",
    kind: "INTERNAL",
    took: "2.9s",
    attrs: ["gen_ai.agent.name=support", "threads.parked"],
    note: "turn 1 · ends where it parks",
    children: [
      {
        id: "2b70ef11",
        op: "chat",
        arg: "claude-sonnet-5",
        kind: "CLIENT",
        took: "1.2s",
        attrs: ["gen_ai.request.model=claude-sonnet-5", "gen_ai.usage.input_tokens=3332"],
      },
      {
        id: "91ac5d40",
        op: "execute_tool",
        arg: "lookup_order",
        kind: "INTERNAL",
        took: "0.3s",
        attrs: ["gen_ai.tool.name=lookup_order", "gen_ai.tool.call.id=call_5b1"],
      },
      {
        id: "6e0b12b9",
        op: "chat",
        arg: "claude-sonnet-5",
        kind: "CLIENT",
        took: "1.1s",
        attrs: ["gen_ai.response.finish_reasons=[tool_use]", "gen_ai.usage.output_tokens=74"],
      },
      {
        id: "d7f349c2",
        op: "execute_tool",
        arg: "mcp__billing__refund",
        kind: "INTERNAL",
        took: "0.2s",
        attrs: ["threads.parked", "threads.tool.effect_class=external"],
        events: ["permission_decision", "approval_requested"],
      },
    ],
  },
  {
    id: "8a15cc03",
    op: "invoke_agent",
    arg: "support",
    kind: "INTERNAL",
    took: "1.4s",
    attrs: ["gen_ai.conversation.id=thr_01JBQ4Z7", "threads.seq.start=13"],
    note: "turn 2 · same trace, linked to turn 1, six minutes later",
    children: [
      {
        id: "33be9077",
        op: "execute_tool",
        arg: "mcp__billing__refund",
        kind: "INTERNAL",
        took: "0.6s",
        attrs: ["threads.resumed", "gen_ai.tool.call.id=call_7g2"],
        events: ["effect_begin", "effect_commit"],
      },
      {
        id: "5fd2a418",
        op: "chat",
        arg: "claude-sonnet-5",
        kind: "CLIENT",
        took: "0.8s",
        attrs: ["gen_ai.response.finish_reasons=[end_turn]", "gen_ai.usage.output_tokens=61"],
      },
    ],
  },
];

/** Every one of these takes OTLP/HTTP JSON, which is what the exporter sends. */
const BACKENDS = ["Honeycomb", "Datadog", "Langfuse", "Grafana", "Maple", "any OTel collector"];

function Chip({ children }: { children: string }) {
  return (
    <span className="rounded border border-fd-border bg-fd-background px-1.5 py-px font-mono text-[0.66rem] whitespace-nowrap text-fd-muted-foreground">
      {children}
    </span>
  );
}

function SpanRow({ span, last }: { span: Span; last: boolean }) {
  const children = span.children ?? [];
  return (
    <li className="min-w-0">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 py-1.5">
        <span className="font-mono text-[0.8rem]">
          <span className="text-fd-primary">{span.op}</span>{" "}
          <span className="text-fd-foreground">{span.arg}</span>
        </span>
        <span className="font-mono text-[0.62rem] tracking-wide text-fd-muted-foreground">{span.kind}</span>
        <span className="ml-auto font-mono text-[0.72rem] tabular-nums text-fd-muted-foreground">
          {span.took}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1 pb-2">
        {span.attrs.map((a) => (
          <Chip key={a}>{a}</Chip>
        ))}
        {span.events?.map((e) => (
          <span
            key={e}
            className="rounded border border-fd-primary/35 bg-fd-primary/5 px-1.5 py-px font-mono text-[0.66rem] whitespace-nowrap text-fd-primary"
          >
            ◆ {e}
          </span>
        ))}
      </div>
      {children.length > 0 ? (
        <ol className="relative ml-1.5">
          <span
            aria-hidden="true"
            className={`absolute top-0 left-0 w-px bg-fd-border ${last ? "h-[calc(100%-1.6rem)]" : "h-full"}`}
          />
          {children.map((c, i) => (
            <li key={c.id} className="relative min-w-0 pl-5">
              <span aria-hidden="true" className="absolute top-[0.95rem] left-0 h-px w-3.5 bg-fd-border" />
              <SpanRow span={c} last={i === children.length - 1} />
            </li>
          ))}
        </ol>
      ) : null}
    </li>
  );
}

export function Trace() {
  return (
    <div className="min-w-0 overflow-hidden rounded-2xl border border-fd-border bg-fd-card">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-fd-border px-4 py-2.5 text-xs">
        <span className="font-medium text-fd-foreground">One trace, the run above</span>
        <span className="ml-auto font-mono text-fd-muted-foreground">trace 8f21c0d4…</span>
      </div>
      <ol className="overflow-x-auto px-4 py-3">
        {TRACE.map((s) => (
          <li key={s.id} className="min-w-0 border-fd-border not-last:mb-2 not-last:border-b">
            <p className="pt-1 font-mono text-[0.68rem] tracking-wide text-fd-muted-foreground">{s.note}</p>
            <ol>
              <SpanRow span={s} last />
            </ol>
          </li>
        ))}
      </ol>
      <p className="border-t border-fd-border px-4 py-3 text-xs leading-5 text-fd-muted-foreground">
        Every span carries <code className="font-mono text-fd-foreground">threads.thread_id</code>,{" "}
        <code className="font-mono text-fd-foreground">threads.branch_id</code> and the log positions it
        covers, so a span in your tracing tool points straight back at the events that produced it.
      </p>
    </div>
  );
}

export function Backends() {
  return (
    <ul className="flex flex-wrap gap-2">
      {BACKENDS.map((b) => (
        <li
          key={b}
          className="rounded-full border border-fd-border bg-fd-background px-3 py-1 text-xs text-fd-muted-foreground"
        >
          {b}
        </li>
      ))}
    </ul>
  );
}

import { gunzipSync } from "node:zlib";
import { z } from "zod";

// A local fake OTLP/HTTP collector: it records every request and answers as the test says. Tests
// never reach a real collector.

const Value = z.object({
  stringValue: z.string().optional(),
  intValue: z.string().optional(),
  boolValue: z.boolean().optional(),
  arrayValue: z.unknown().optional(),
});
const Attr = z.object({ key: z.string(), value: Value });
const WireSpan = z.object({
  traceId: z.string(),
  spanId: z.string(),
  parentSpanId: z.string().optional(),
  name: z.string(),
  attributes: z.array(Attr),
  status: z.object({ code: z.int(), message: z.string() }).optional(),
});
const Body = z.object({
  resourceSpans: z.array(
    z.object({
      resource: z.object({ attributes: z.array(Attr) }),
      scopeSpans: z.array(z.object({ spans: z.array(WireSpan) })),
    }),
  ),
});
export type WireSpan = {
  readonly traceId: string;
  readonly spanId: string;
  readonly parentSpanId?: string | undefined;
  readonly name: string;
  readonly attributes: readonly {
    readonly key: string;
    readonly value: {
      readonly stringValue?: string | undefined;
      readonly intValue?: string | undefined;
      readonly boolValue?: boolean | undefined;
      readonly arrayValue?: unknown;
    };
  }[];
  readonly status?:
    | { readonly code: number; readonly message: string }
    | undefined;
};

export type Received = {
  readonly headers: Headers;
  /** The body as sent (gzip-decoded when it was compressed). */
  readonly text: string;
  readonly spans: readonly WireSpan[];
};

/** How the collector answers the next request: a status, or never. */
export type Answer = number | "hang";

export type Collector = {
  readonly url: string;
  /** Requests the collector accepted. */
  readonly received: Received[];
  /** Every request body, accepted or not. */
  readonly attempts: string[];
  /** Answers, in order; once used up, every request gets 200. */
  answers: Answer[];
  readonly stop: () => Promise<void>;
};

export function attr(s: WireSpan, key: string): string | boolean | undefined {
  const v = s.attributes.find((a) => a.key === key)?.value;
  return v?.stringValue ?? v?.intValue ?? v?.boolValue;
}

export function collector(): Collector {
  const received: Received[] = [];
  const attempts: string[] = [];
  const hanging: (() => void)[] = [];
  const state: { answers: Answer[] } = { answers: [] };
  const server = Bun.serve({
    port: 0,
    fetch: async (request) => {
      const raw = new Uint8Array(await request.arrayBuffer());
      const gzipped = request.headers.get("content-encoding") === "gzip";
      const text = new TextDecoder().decode(gzipped ? gunzipSync(raw) : raw);
      attempts.push(text);
      const answer = state.answers.shift() ?? 200;
      if (answer === "hang")
        return await new Promise<Response>((resolve) => {
          hanging.push(() => resolve(new Response("", { status: 503 })));
        });
      if (answer >= 200 && answer < 300) {
        const body = Body.parse(JSON.parse(text));
        const spans = body.resourceSpans.flatMap((r) =>
          r.scopeSpans.flatMap((s) => s.spans),
        );
        received.push({ headers: request.headers, text, spans });
      }
      return new Response(answer >= 400 ? "bad spans: missing traceId" : "", {
        status: answer,
      });
    },
  });
  return {
    url: `http://127.0.0.1:${server.port}/v1/traces`,
    received,
    attempts,
    get answers(): Answer[] {
      return state.answers;
    },
    set answers(next: Answer[]) {
      state.answers = next;
    },
    stop: async () => {
      for (const release of hanging) release();
      await server.stop(true);
    },
  };
}

/** Every span the collector accepted, in order. */
export function accepted(c: Collector): readonly WireSpan[] {
  return c.received.flatMap((r) => r.spans);
}

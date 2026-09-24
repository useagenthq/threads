import { z } from "zod";
import {
  ModelAttemptAbandonedData,
  ModelResponseData,
  OutputPart,
  Usage,
} from "../log";
import { ok } from "../result";
import { markTestKit } from "./guard";
import type {
  LookupResult,
  Model,
  ModelChunk,
  ModelInfo,
  ModelResponse,
} from "./protocol";

// scriptedModel (spec/api.json): plays spec/conformance ModelScript, $defs/ModelScript in
// case.schema.json. Scripts are files, so they are parsed at the boundary.

const Reply = z.strictObject({
  content: z.array(OutputPart),
  stop_reason: ModelResponseData.shape.stop_reason,
  usage: Usage,
});
const Rejection = z.strictObject({
  error: z.strictObject({
    reason: ModelAttemptAbandonedData.shape.reason.extract([
      "rate_limited",
      "overloaded",
      "server_error",
      "prompt_too_long",
    ]),
    http_status: z.int(),
    retry_after_ms: z.int().optional(),
  }),
});
const Script = z.strictObject({
  responses: z.array(z.union([Reply, Rejection])),
  lookup: z
    .record(
      z.string(),
      z.strictObject({
        result: z.enum(["found", "not_found"]),
        final: z.boolean(),
        provider_request_id: z.string().optional(),
        response: Reply.optional(),
      }),
    )
    .optional(),
});

type Entry =
  | z.infer<typeof Reply>
  | z.infer<typeof Rejection>
  | {
      readonly error: {
        readonly reason: "provider_error";
        readonly http_status: number;
      };
    };
type Answer = NonNullable<z.infer<typeof Script>["lookup"]>[string];

/**
 * A scripted model. `remaining()` counts responses never asked for; `unexpected()` counts calls
 * past the end of the script.
 */
export type ScriptedModel = Model & {
  readonly remaining: () => number;
  readonly unexpected: () => number;
};

const INFO: ModelInfo = {
  model: { provider: "scripted", name: "scripted-1" },
  adapter: { name: "scripted", version: "1", settings: {} },
  params: { max_tokens: 1024 },
  limits: {
    provider: "scripted",
    name: "scripted-1",
    context_window: 200_000,
    max_output_tokens: 8192,
    input_billing_bound: "context_window",
  },
  accepts: ["text"],
  lookup: "none",
  cache: "none",
};

/**
 * Test kit. Consumes the script in order. A call past the end is rejected as a provider error,
 * which fails the run (turn_completed{error}); the exact requests are in the log. A lookup
 * answers by model_request event id.
 */
export function scriptedModel(script: unknown): ScriptedModel {
  const parsed = Script.parse(script);
  const queue: Entry[] = [...parsed.responses];
  const answers = parsed.lookup;
  let unexpected = 0;
  const next = (): Entry => {
    const entry = queue.shift();
    if (entry !== undefined) return entry;
    unexpected += 1;
    return { error: { reason: "provider_error", http_status: 400 } };
  };
  const model: ScriptedModel = {
    info: { ...INFO, lookup: answers === undefined ? "none" : "final" },
    send: () => play(next()),
    remaining: () => queue.length,
    unexpected: () => unexpected,
    ...(answers === undefined
      ? {}
      : {
          lookup: async (requestId: string) =>
            ok(lookupAnswer(answers[eventIdOf(requestId)])),
        }),
  };
  markTestKit(model);
  return model;
}

/** The client request id is `<branch_id>:<event_id>`; the script keys by event id. */
function eventIdOf(requestId: string): string {
  return requestId.slice(requestId.indexOf(":") + 1);
}

async function* play(
  entry: Entry,
): AsyncGenerator<ModelChunk, void, undefined> {
  if ("error" in entry) {
    yield { kind: "rejected", ...entry.error };
    return;
  }
  for (const part of entry.content) {
    if (part.type === "text") yield { kind: "delta", text: part.text };
    yield { kind: "part", part };
  }
  yield { kind: "done", stop_reason: entry.stop_reason, usage: entry.usage };
}

function lookupAnswer(answer: Answer | undefined): LookupResult<ModelResponse> {
  if (answer === undefined)
    return { status: "unknown", reason: "no scripted lookup" };
  if (answer.result === "not_found")
    return { status: answer.final ? "not_found" : "not_found_nonfinal" };
  const { response, provider_request_id: id } = answer;
  if (!answer.final || response === undefined)
    return { status: "unknown", reason: "found without a final response" };
  return {
    status: "found",
    value: { ...response, provider_request_id: id ?? null },
  };
}

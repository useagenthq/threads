import type {
  LanguageModelV4FinishReason,
  LanguageModelV4StreamPart,
  LanguageModelV4Usage,
} from "@ai-sdk/provider";
import type { ModelChunk, ModelContext, Usage } from "@threads/core/adapter";
import { JsonObject, JsonValue, OutputPart } from "@threads/core/adapter";
import { ProviderOptions, type ReplayPart } from "./replay";

// AI SDK stream parts → ModelChunks. Text streams as deltas and lands as one part at text-end;
// reasoning and provider-executed tool parts are stored as the exact AI SDK part to send back.

type Stop = Extract<ModelChunk, { kind: "done" }>["stop_reason"];
type Part = LanguageModelV4StreamPart;

export type StreamContext = {
  readonly provider: string;
  readonly model: string;
  readonly context: ModelContext;
};

/** An error part from the provider, before or after content. */
export class StreamError extends Error {
  readonly error: unknown;
  constructor(error: unknown) {
    super("the provider stream reported an error");
    this.error = error;
  }
}

const encoder = new TextEncoder();
const STOPS: Record<LanguageModelV4FinishReason["unified"], Stop> = {
  stop: "end_turn",
  length: "max_tokens",
  "tool-calls": "tool_use",
  "content-filter": "refusal",
  error: "other",
  other: "other",
};

export async function* decode(
  stream: ReadableStream<Part>,
  ctx: StreamContext,
): AsyncGenerator<ModelChunk, void, undefined> {
  const open = new Map<string, string>();
  for await (const part of stream) {
    if (part.type === "finish") {
      yield {
        kind: "done",
        stop_reason: STOPS[part.finishReason.unified],
        usage: toUsage(part.usage),
      };
      return;
    }
    if (part.type === "error") throw new StreamError(part.error);
    yield* chunks(part, open, ctx);
  }
  throw new Error("ai-sdk stream ended before finish");
}

async function* chunks(
  part: Part,
  open: Map<string, string>,
  ctx: StreamContext,
): AsyncGenerator<ModelChunk, void, undefined> {
  switch (part.type) {
    case "text-start":
    case "reasoning-start":
      open.set(part.id, "");
      return;
    case "text-delta":
      open.set(part.id, `${open.get(part.id) ?? ""}${part.delta}`);
      yield { kind: "delta", text: part.delta };
      return;
    case "reasoning-delta":
      open.set(part.id, `${open.get(part.id) ?? ""}${part.delta}`);
      return;
    case "text-end":
      yield text(open.get(part.id) ?? "");
      open.delete(part.id);
      return;
    case "reasoning-end":
      yield await reasoning(
        open.get(part.id) ?? "",
        part.providerMetadata,
        ctx,
      );
      open.delete(part.id);
      return;
    case "tool-call":
    case "tool-result":
    case "source":
      yield { kind: "part", part: await recorded(part, ctx) };
      return;
    default:
      // Progress (tool-input-*, stream-start, response-metadata, raw) records nothing.
      return;
  }
}

function text(value: string): ModelChunk {
  return {
    kind: "part",
    part: OutputPart.parse({ type: "text", text: value }),
  };
}

async function reasoning(
  value: string,
  metadata: unknown,
  ctx: StreamContext,
): Promise<ModelChunk> {
  const replay: ReplayPart = {
    type: "reasoning",
    text: value,
    ...options(metadata),
  };
  return {
    kind: "part",
    part: OutputPart.parse({
      type: "reasoning",
      provider: ctx.provider,
      model: ctx.model,
      format: "ai_sdk_reasoning",
      ref: await store(replay, ctx.context),
      ...(value === "" ? {} : { summary: value }),
    }),
  };
}

async function recorded(
  part: Extract<Part, { type: "tool-call" | "tool-result" | "source" }>,
  ctx: StreamContext,
): Promise<OutputPart> {
  if (part.type === "source")
    return OutputPart.parse(
      part.sourceType === "url"
        ? {
            type: "citation",
            source_kind: "web",
            source_id: part.url,
            ...(part.title === undefined ? {} : { title: part.title }),
          }
        : {
            type: "citation",
            source_kind: "document",
            source_id: part.id,
            title: part.title,
          },
    );
  const input = JsonObject.parse(
    part.type === "tool-call" ? JSON.parse(part.input) : {},
  );
  if (part.type === "tool-call" && part.providerExecuted !== true)
    return OutputPart.parse({
      type: "tool_use",
      call_id: part.toolCallId,
      name: part.toolName,
      input,
    });
  // Provider-executed: kept whole and never dispatched locally.
  const replay: ReplayPart =
    part.type === "tool-call"
      ? {
          type: "tool-call",
          toolCallId: part.toolCallId,
          toolName: part.toolName,
          input,
          providerExecuted: true,
          ...options(part.providerMetadata),
        }
      : {
          type: "tool-result",
          toolCallId: part.toolCallId,
          toolName: part.toolName,
          output: { type: "json", value: JsonValue.parse(part.result) },
          ...options(part.providerMetadata),
        };
  return OutputPart.parse({
    type: "hosted_tool",
    provider: ctx.provider,
    model: ctx.model,
    format:
      part.type === "tool-call" ? "ai_sdk_tool_call" : "ai_sdk_tool_result",
    name: nameOf(part.toolName),
    ref: await store(replay, ctx.context),
  });
}

/** A wire Name (^[a-z][a-z0-9_]{0,63}$) from a provider's own identifier. */
export function nameOf(value: string): string {
  const name = value.toLowerCase().replaceAll(/[^a-z0-9_]/g, "_");
  return (/^[a-z]/.test(name) ? name : `p_${name}`).slice(0, 64);
}

/** Provider metadata goes back as provider options: it carries signatures and item ids. */
function options(metadata: unknown): Pick<ReplayPart, "providerOptions"> {
  return metadata === undefined
    ? {}
    : { providerOptions: ProviderOptions.parse(metadata) };
}

function store(
  replay: ReplayPart,
  context: ModelContext,
): ReturnType<ModelContext["put"]> {
  return context.put(
    encoder.encode(JSON.stringify(replay)),
    "application/json",
  );
}

/** Unknown is null, never 0. noCache is input without cache reads or writes. */
function toUsage(u: LanguageModelV4Usage): Usage {
  return {
    input_tokens: u.inputTokens.noCache ?? null,
    output_tokens: u.outputTokens.total ?? null,
    cache_read_tokens: u.inputTokens.cacheRead ?? null,
    cache_write_tokens: u.inputTokens.cacheWrite ?? null,
    reasoning_tokens: u.outputTokens.reasoning ?? null,
  };
}

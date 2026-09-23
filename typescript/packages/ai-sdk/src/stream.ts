import type {
  LanguageModelV4FinishReason,
  LanguageModelV4StreamPart,
  LanguageModelV4Usage,
} from "@ai-sdk/provider";
import type { ModelChunk, ModelContext, Usage } from "@threads/core/adapter";
import {
  JsonObject,
  JsonValue,
  OutputPart,
  putJson,
} from "@threads/core/adapter";
import {
  METADATA_FORMAT,
  type PartMetadata,
  ProviderOptions,
  type ReplayPart,
} from "./replay";

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
  const open: Open = new Map();
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

/** Open text and reasoning blocks by id: their text so far and the provider metadata seen. */
type Open = Map<string, { text: string; metadata: unknown }>;

async function* chunks(
  part: Part,
  open: Open,
  ctx: StreamContext,
): AsyncGenerator<ModelChunk, void, undefined> {
  switch (part.type) {
    case "text-start":
    case "reasoning-start":
      open.set(part.id, { text: "", metadata: part.providerMetadata });
      return;
    case "text-delta":
    case "reasoning-delta":
      grow(open, part.id, part.delta, part.providerMetadata);
      if (part.type === "text-delta") yield { kind: "delta", text: part.delta };
      return;
    case "text-end":
    case "reasoning-end": {
      const block = grow(open, part.id, "", part.providerMetadata);
      open.delete(part.id);
      if (part.type === "reasoning-end")
        yield await reasoning(block.text, block.metadata, ctx);
      else yield* withMetadata(block.metadata, text(block.text), ctx);
      return;
    }
    case "tool-call":
    case "tool-result":
    case "source": {
      const local = part.type === "tool-call" && part.providerExecuted !== true;
      const recordedPart = await recorded(part, ctx);
      yield* local
        ? withMetadata(part.providerMetadata, recordedPart, ctx)
        : [{ kind: "part", part: recordedPart }];
      return;
    }
    default:
      // Progress (tool-input-*, stream-start, response-metadata, raw) records nothing.
      return;
  }
}

/** Appends to an open block; the latest metadata the provider sent wins. */
function grow(
  open: Open,
  id: string,
  more: string,
  metadata: unknown,
): { text: string; metadata: unknown } {
  const block = open.get(id) ?? { text: "", metadata: undefined };
  const next = {
    text: `${block.text}${more}`,
    metadata: metadata ?? block.metadata,
  };
  open.set(id, next);
  return next;
}

/**
 * A text or local tool call part, preceded by its provider metadata as an opaque part when
 * there is some, so the next request can attach it again (Gemini thought signatures).
 */
async function* withMetadata(
  metadata: unknown,
  part: OutputPart,
  ctx: StreamContext,
): AsyncGenerator<ModelChunk, void, undefined> {
  if (metadata !== undefined) {
    const stored: PartMetadata = {
      type: "metadata",
      providerOptions: ProviderOptions.parse(metadata),
    };
    yield {
      kind: "part",
      part: OutputPart.parse({
        type: "reasoning",
        provider: ctx.provider,
        model: ctx.model,
        format: METADATA_FORMAT,
        ref: await putJson(ctx.context, stored),
      }),
    };
  }
  yield { kind: "part", part };
}

function text(value: string): OutputPart {
  return OutputPart.parse({ type: "text", text: value });
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
      ref: await putJson(ctx.context, replay),
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
    ref: await putJson(ctx.context, replay),
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

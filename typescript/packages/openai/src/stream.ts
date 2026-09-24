import type { ModelChunk, Usage } from "@threads/core/adapter";
import { assertNever, JsonObject } from "@threads/core/adapter";
import { z } from "zod";
import { type ItemContext, itemParts } from "./items";

// Responses API stream events → ModelChunks. Events are provider output, so each is parsed;
// event types this adapter doesn't need (progress, per-part deltas) are skipped.

const Tokens = z.int().nonnegative();
const ProviderUsage = z.object({
  input_tokens: Tokens,
  output_tokens: Tokens,
  input_tokens_details: z
    .object({
      cached_tokens: Tokens.nullish(),
      cache_write_tokens: Tokens.nullish(),
    })
    .nullish(),
  output_tokens_details: z
    .object({ reasoning_tokens: Tokens.nullish() })
    .nullish(),
});
const Final = z.object({
  usage: ProviderUsage.nullish(),
  incomplete_details: z.object({ reason: z.string().nullish() }).nullish(),
  error: z
    .object({ code: z.string().nullish(), message: z.string() })
    .nullish(),
});
const Event = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("response.output_text.delta"),
    content_index: z.int().nonnegative(),
    delta: z.string(),
  }),
  z.object({ type: z.literal("response.output_item.done"), item: JsonObject }),
  z.object({ type: z.literal("response.completed"), response: Final }),
  z.object({ type: z.literal("response.incomplete"), response: Final }),
  z.object({ type: z.literal("response.failed"), response: Final }),
]);
const KNOWN = new Set<string>(Event.options.map((o) => o.shape.type.value));
const Tagged = z.object({ type: z.string() });

type Stop = Extract<ModelChunk, { kind: "done" }>["stop_reason"];

/** response.failed, carrying the provider's error code for the rejection classes. */
export class ResponseFailed extends Error {
  readonly code: string | undefined;
  constructor(code: string | null | undefined, message: string) {
    super(message);
    this.code = code ?? undefined;
  }
}

export async function* decode(
  events: AsyncIterable<unknown>,
  ctx: ItemContext,
): AsyncGenerator<ModelChunk, void, undefined> {
  const seen = { calls: false, refused: false };
  // Parts yielded so far. Items arrive one at a time, so a message's first text is the next
  // part; a later content's index depends on the citations before it, so it streams no deltas.
  let yielded = 0;
  for await (const raw of events) {
    if (!KNOWN.has(Tagged.parse(raw).type)) continue;
    const e = Event.parse(raw);
    switch (e.type) {
      case "response.output_text.delta":
        if (e.content_index === 0)
          yield { kind: "delta", part: yielded, text: e.delta };
        break;
      case "response.output_item.done": {
        const item = await itemParts(e.item, ctx);
        seen.refused ||= item.refused;
        seen.calls ||= item.parts.some((p) => p.type === "tool_use");
        for (const part of item.parts) yield { kind: "part", part };
        yielded += item.parts.length;
        break;
      }
      case "response.completed":
      case "response.incomplete":
      case "response.failed":
        yield finish(e, seen);
        return;
      default:
        assertNever(e);
    }
  }
  throw new Error("openai stream ended before the response finished");
}

type Terminal = Extract<
  z.infer<typeof Event>,
  { type: "response.completed" | "response.incomplete" | "response.failed" }
>;

function finish(
  e: Terminal,
  seen: { readonly calls: boolean; readonly refused: boolean },
): ModelChunk {
  const { response } = e;
  if (e.type === "response.failed")
    throw new ResponseFailed(
      response.error?.code,
      response.error?.message ?? "response failed",
    );
  const stop: Stop =
    e.type === "response.incomplete"
      ? response.incomplete_details?.reason === "max_output_tokens"
        ? "max_tokens"
        : "other"
      : seen.refused
        ? "refusal"
        : seen.calls
          ? "tool_use"
          : "end_turn";
  return { kind: "done", stop_reason: stop, usage: toUsage(response.usage) };
}

/**
 * Unknown is null, never 0. OpenAI's input_tokens counts cached and
 * cache-written tokens; the wire's excludes both.
 */
function toUsage(u: z.infer<typeof ProviderUsage> | null | undefined): Usage {
  if (u === null || u === undefined)
    return {
      input_tokens: null,
      output_tokens: null,
      cache_read_tokens: null,
      cache_write_tokens: null,
      reasoning_tokens: null,
    };
  const read = u.input_tokens_details?.cached_tokens;
  const write = u.input_tokens_details?.cache_write_tokens;
  return {
    // Without both cache counts, the uncached share isn't known.
    input_tokens:
      typeof read === "number" && typeof write === "number"
        ? u.input_tokens - read - write
        : null,
    output_tokens: u.output_tokens,
    cache_read_tokens: read ?? null,
    cache_write_tokens: write ?? null,
    reasoning_tokens: u.output_tokens_details?.reasoning_tokens ?? null,
  };
}

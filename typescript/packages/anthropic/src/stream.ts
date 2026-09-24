import type { Json, ModelChunk, Usage } from "@threads/core/adapter";
import { assertNever, JsonObject } from "@threads/core/adapter";
import { z } from "zod";
import { type BlockContext, blockParts } from "./blocks";
import type { Ttl } from "./caching";

// Messages API stream events → ModelChunks. Events are provider output, so each is parsed; an
// event type this adapter doesn't know is skipped, as the API asks clients to do.

const Tokens = z.int().nonnegative().nullish();
const ProviderUsage = z.object({
  input_tokens: Tokens,
  output_tokens: Tokens,
  cache_read_input_tokens: Tokens,
  cache_creation_input_tokens: Tokens,
  cache_creation: z
    .object({
      ephemeral_5m_input_tokens: Tokens,
      ephemeral_1h_input_tokens: Tokens,
    })
    .nullish(),
  output_tokens_details: z.object({ thinking_tokens: z.int() }).nullish(),
});
type ProviderUsage = z.infer<typeof ProviderUsage>;

const Delta = z.discriminatedUnion("type", [
  z.object({ type: z.literal("text_delta"), text: z.string() }),
  z.object({ type: z.literal("input_json_delta"), partial_json: z.string() }),
  z.object({ type: z.literal("citations_delta"), citation: JsonObject }),
  z.object({ type: z.literal("thinking_delta"), thinking: z.string() }),
  z.object({ type: z.literal("signature_delta"), signature: z.string() }),
]);

const Index = z.int().nonnegative();
const Event = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("message_start"),
    message: z.object({ usage: ProviderUsage }),
  }),
  z.object({
    type: z.literal("content_block_start"),
    index: Index,
    content_block: JsonObject,
  }),
  z.object({
    type: z.literal("content_block_delta"),
    index: Index,
    delta: Delta,
  }),
  z.object({ type: z.literal("content_block_stop"), index: Index }),
  z.object({
    type: z.literal("message_delta"),
    delta: z.object({ stop_reason: z.string().nullish() }),
    usage: ProviderUsage,
  }),
  z.object({ type: z.literal("message_stop") }),
]);
const KNOWN = new Set<string>(Event.options.map((o) => o.shape.type.value));
const Tagged = z.object({ type: z.string() });

type Stop = Extract<ModelChunk, { kind: "done" }>["stop_reason"];
// A reason the SDK doesn't list yet is "other", which ends the turn with error, never success.
const STOPS = new Map<string, Stop>([
  ["end_turn", "end_turn"],
  ["tool_use", "tool_use"],
  ["max_tokens", "max_tokens"],
  ["stop_sequence", "stop_sequence"],
  ["refusal", "refusal"],
  ["pause_turn", "pause_turn"],
  ["model_context_window_exceeded", "context_window_exceeded"],
]);

type Open = { block: { [key: string]: Json }; json: string };

export async function* decode(
  events: AsyncIterable<unknown>,
  ctx: BlockContext,
  ttl: Ttl | undefined,
): AsyncGenerator<ModelChunk, void, undefined> {
  const open = new Map<number, Open>();
  let usage: ProviderUsage = {};
  let stop: Stop = "other";
  for await (const raw of events) {
    if (!KNOWN.has(Tagged.parse(raw).type)) continue;
    const e = Event.parse(raw);
    switch (e.type) {
      case "message_start":
        usage = e.message.usage;
        break;
      case "content_block_start":
        open.set(e.index, { block: { ...e.content_block }, json: "" });
        break;
      case "content_block_delta": {
        const text = apply(openAt(open, e.index), e.delta);
        if (text !== undefined) yield { kind: "delta", text };
        break;
      }
      case "content_block_stop":
        for (const part of await blockParts(close(openAt(open, e.index)), ctx))
          yield { kind: "part", part };
        open.delete(e.index);
        break;
      case "message_delta":
        usage = merge(usage, e.usage);
        stop = stopOf(e.delta.stop_reason);
        break;
      case "message_stop":
        yield { kind: "done", stop_reason: stop, usage: toUsage(usage, ttl) };
        return;
      default:
        assertNever(e);
    }
  }
  throw new Error("anthropic stream ended before message_stop");
}

function openAt(open: Map<number, Open>, index: number): Open {
  const block = open.get(index);
  if (block === undefined) throw new Error(`no open content block ${index}`);
  return block;
}

function stopOf(reason: string | null | undefined): Stop {
  return STOPS.get(reason ?? "") ?? "other";
}

/** Applies one delta to its open block; returns streamed text, if any. */
function apply(o: Open, delta: z.infer<typeof Delta>): string | undefined {
  const text = (key: string, more: string): void => {
    const was = o.block[key];
    o.block[key] = `${typeof was === "string" ? was : ""}${more}`;
  };
  switch (delta.type) {
    case "text_delta":
      text("text", delta.text);
      return delta.text;
    case "thinking_delta":
      text("thinking", delta.thinking);
      return undefined;
    case "signature_delta":
      o.block["signature"] = delta.signature;
      return undefined;
    case "citations_delta": {
      const was = o.block["citations"];
      o.block["citations"] = [
        ...(Array.isArray(was) ? was : []),
        delta.citation,
      ];
      return undefined;
    }
    case "input_json_delta":
      o.json += delta.partial_json;
      return undefined;
    default:
      return assertNever(delta);
  }
}

/** Streamed tool input arrives as JSON text; an empty stream leaves the start's input. */
function close(o: Open): { readonly [key: string]: Json } {
  return o.json === ""
    ? o.block
    : { ...o.block, input: JsonObject.parse(JSON.parse(o.json)) };
}

/** message_delta usage is cumulative; a field it leaves out keeps message_start's value. */
function merge(a: ProviderUsage, b: ProviderUsage): ProviderUsage {
  return {
    input_tokens: b.input_tokens ?? a.input_tokens,
    output_tokens: b.output_tokens ?? a.output_tokens,
    cache_read_input_tokens:
      b.cache_read_input_tokens ?? a.cache_read_input_tokens,
    cache_creation_input_tokens:
      b.cache_creation_input_tokens ?? a.cache_creation_input_tokens,
    cache_creation: b.cache_creation ?? a.cache_creation,
    output_tokens_details: b.output_tokens_details ?? a.output_tokens_details,
  };
}

/** Unknown is null, never 0. input_tokens already excludes cache. */
function toUsage(u: ProviderUsage, ttl: Ttl | undefined): Usage {
  return {
    input_tokens: u.input_tokens ?? null,
    output_tokens: u.output_tokens ?? null,
    cache_read_tokens: u.cache_read_input_tokens ?? null,
    cache_write_tokens: pricedWrites(u, ttl),
    reasoning_tokens: u.output_tokens_details?.thinking_tokens ?? null,
  };
}

/**
 * Cache writes all billed at the pinned TTL's price, or null (unknown) when the provider reports
 * any under the other TTL: a number priced at the wrong rate would understate the cost.
 */
function pricedWrites(u: ProviderUsage, ttl: Ttl | undefined): number | null {
  const other =
    ttl === "1h"
      ? u.cache_creation?.ephemeral_5m_input_tokens
      : ttl === "5m"
        ? u.cache_creation?.ephemeral_1h_input_tokens
        : undefined;
  return (other ?? 0) > 0 ? null : (u.cache_creation_input_tokens ?? null);
}

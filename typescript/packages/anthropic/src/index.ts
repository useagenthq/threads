import Anthropic, {
  APIConnectionError,
  APIError,
  APIUserAbortError,
} from "@anthropic-ai/sdk";
import type { Stream } from "@anthropic-ai/sdk/core/streaming";
import type {
  Fetch,
  LimitOptions,
  Model,
  ModelChunk,
  ModelContext,
  ModelInfo,
  ModelRequest,
  Price,
  Secret,
} from "@threads/core/adapter";
import {
  ConfigError,
  checkHostedTools,
  credential,
  fencedFetch,
  type JsonObject,
  modelLimits,
  parseRender,
  rejectionFor,
  staleEpoch,
} from "@threads/core/adapter";

import {
  cacheInfo,
  cachePrice,
  type PromptCache,
  promptCache,
  refuseCacheControl,
} from "./caching";
import { toAnthropic } from "./request";
import { decode } from "./stream";

export type { JsonObject, Price } from "@threads/core/adapter";
export type { PromptCache } from "./caching";

// anthropic(): the Messages API through the official SDK, as a threads Model (spec/api.json).
// threads owns every attempt, so SDK retries are off, and the request body is
// derived from the Render v1 bytes alone.

type Rejected = Extract<ModelChunk, { kind: "rejected" }>;

/** The limits default from spec/models/anthropic.v1.json when the model id is listed there. */
export type AnthropicOptions = LimitOptions & {
  /** Other Messages API parameters (thinking, temperature, output_config, ...), pinned in line 0. */
  readonly params?: JsonObject;
  readonly price?: Price;
  /** Provider-executed tools (web search, web fetch), sent as recorded here. */
  readonly hostedTools?: readonly JsonObject[];
  /**
   * Prompt caching, pinned in line 0: the end of line 0 and the growing history are cached for
   * this long. Defaults to "5m"; false sends no cache controls (and continues threads started
   * before prompt caching existed).
   */
  readonly promptCache?: PromptCache;
  /** Citations on every document in the request, pinned in line 0. Defaults to off. */
  readonly citations?: boolean;
  /** Defaults to secret("ANTHROPIC_API_KEY"), resolved at setup. Never pinned or logged. */
  readonly apiKey?: string | Secret;
  readonly baseUrl?: string;
  readonly fetch?: Fetch;
};

const RESERVED = [
  "model",
  "messages",
  "system",
  "tools",
  "stream",
  "max_tokens",
];
const ADAPTER = { name: "anthropic", version: "1" } as const;
/** Server tools read-only toward the outside world: web search and web fetch. */
const HOSTED_READ_ONLY = /^(web_search|web_fetch)_\d{8}$/;

/** A Claude model by its exact id: anthropic("claude-sonnet-5"). */
export function anthropic(
  model: string,
  options: AnthropicOptions = {},
): Model {
  const params = options.params ?? {};
  const clash = RESERVED.find((key) => Object.hasOwn(params, key));
  if (clash !== undefined)
    throw new ConfigError(
      "invalid_config",
      clash === "max_tokens"
        ? "anthropic params can't set max_tokens: pass maxTokens"
        : `anthropic params can't set ${clash}: the adapter derives it from the render`,
    );
  const hosted = options.hostedTools ?? [];
  checkHostedTools("anthropic", hosted, HOSTED_READ_ONLY);
  refuseCacheControl(params, hosted);
  const ttl = promptCache(options.promptCache, hosted);
  const citations = options.citations === true;
  if (citations) refuseStructuredOutput(params);
  const limits = modelLimits("anthropic", model, options);
  const price = cachePrice(options.price, ttl);
  const info: ModelInfo = {
    model: { provider: "anthropic", name: model },
    adapter: {
      ...ADAPTER,
      settings: {
        ...(hosted.length === 0 ? {} : { hosted_tools: [...hosted] }),
        ...(ttl === undefined ? {} : { prompt_cache: ttl }),
        ...(citations ? { citations: true } : {}),
      },
    },
    params: { ...params, max_tokens: limits.max_tokens },
    limits: {
      provider: "anthropic",
      name: model,
      context_window: limits.max_input_tokens,
      max_output_tokens: limits.max_output_tokens,
      input_billing_bound: "context_window",
      ...(price === undefined ? {} : { price }),
    },
    accepts: ["text", "image_ref", "document_ref"],
    hosted_tools: hosted.map((t) => String(t["name"] ?? t["type"])),
    // The Messages API has no retrieval by client request id.
    lookup: "none",
    cache: cacheInfo(ttl),
  };
  const apiKey = credential(
    "anthropic",
    "apiKey",
    options.apiKey,
    "ANTHROPIC_API_KEY",
  );
  return {
    info,
    setup: async () => {
      apiKey();
    },
    send: (request, context, sendOptions) =>
      send(options, apiKey, request, context, sendOptions?.signal),
  };
}

/** The provider refuses citations with native structured output (output_config.format). */
function refuseStructuredOutput(params: JsonObject): void {
  const config = params["output_config"];
  const format =
    Object.hasOwn(params, "output_format") ||
    (typeof config === "object" &&
      config !== null &&
      !Array.isArray(config) &&
      Object.hasOwn(config, "format"));
  if (format)
    throw new ConfigError(
      "invalid_config",
      "anthropic citations can't be combined with structured output (output_format or output_config.format in params): drop one, or use agent output, which is unaffected",
    );
}

async function* send(
  options: AnthropicOptions,
  apiKey: () => string,
  request: ModelRequest,
  context: ModelContext,
  signal: AbortSignal | undefined,
): AsyncGenerator<ModelChunk, void, undefined> {
  const render = parseRender(request.body);
  const mapped = await toAnthropic(render, context);
  if (!mapped.ok) {
    // Refused before dispatch: nothing was sent.
    yield { kind: "rejected", reason: mapped.code };
    return;
  }
  const client = new Anthropic({
    apiKey: apiKey(),
    ...(options.baseUrl === undefined ? {} : { baseURL: options.baseUrl }),
    maxRetries: 0,
    fetch: fencedFetch(context, options.fetch ?? fetch),
  });
  let yielded = false;
  try {
    const events = await client.post<Stream<unknown>>("/v1/messages", {
      body: mapped.body,
      stream: true,
      ...(signal === undefined ? {} : { signal }),
    });
    const ctx = {
      model: render.head.model.name,
      context,
      documents: mapped.documents,
    };
    for await (const chunk of decode(events, ctx, mapped.ttl)) {
      yielded = true;
      yield chunk;
    }
  } catch (error) {
    const rejected = yielded ? undefined : rejection(error);
    if (staleEpoch(error) !== undefined) {
      // The fence refused at the real send point: nothing left (in-band, never a throw).
      yield { kind: "rejected", reason: "stale_epoch" };
      return;
    }
    if (rejected === undefined) throw error;
    yield rejected;
  }
}

const STREAM_ERRORS = new Map<string, Rejected["reason"]>([
  ["overloaded_error", "overloaded"],
  ["rate_limit_error", "rate_limited"],
  ["api_error", "server_error"],
]);

/**
 * A failure before any content, as its class. A connection failure or abort is
 * not a rejection: the request may have left, so it stays unknown (rethrown).
 */
function rejection(error: unknown): Rejected | undefined {
  if (
    !(error instanceof APIError) ||
    error instanceof APIConnectionError ||
    error instanceof APIUserAbortError
  )
    return undefined;
  if (error.status === undefined) {
    const reason = STREAM_ERRORS.get(error.type ?? "");
    return reason === undefined ? undefined : { kind: "rejected", reason };
  }
  return rejectionFor(
    error.status,
    error.headers,
    {
      promptTooLong:
        error.status === 400 && /prompt is too long/i.test(error.message),
      retryable: true,
    },
    Date.now(),
  );
}

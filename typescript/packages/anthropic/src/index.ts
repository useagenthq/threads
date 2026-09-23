import Anthropic, {
  APIConnectionError,
  APIError,
  APIUserAbortError,
} from "@anthropic-ai/sdk";
import type { Stream } from "@anthropic-ai/sdk/core/streaming";
import type {
  Fetch,
  Model,
  ModelChunk,
  ModelContext,
  ModelInfo,
  ModelRequest,
  Secret,
} from "@threads/core/adapter";
import {
  ConfigError,
  checkHostedTools,
  credential,
  fencedFetch,
  type JsonObject,
  parseRender,
  rejectionFor,
  staleEpoch,
} from "@threads/core/adapter";

import { toAnthropic } from "./request";
import { decode } from "./stream";

// anthropic(): the Messages API through the official SDK, as a threads Model (spec/api.json).
// threads owns every attempt, so SDK retries are off, and the request body is
// derived from the Render v1 bytes alone.

type Rejected = Extract<ModelChunk, { kind: "rejected" }>;

export type AnthropicOptions = {
  /** The model id, e.g. "claude-sonnet-5". No default. */
  readonly model: string;
  /** Sent as max_tokens. */
  readonly maxTokens: number;
  /** The model's declared context window and output cap, as the Models API reports them. */
  readonly contextWindow: number;
  readonly maxOutputTokens: number;
  /** Other Messages API parameters (thinking, temperature, output_config, ...), pinned in line 0. */
  readonly params?: JsonObject;
  readonly price?: ModelInfo["limits"]["price"];
  /** Provider-executed tools (web search, code execution), sent as recorded here. */
  readonly hostedTools?: readonly JsonObject[];
  /** Defaults to secret("ANTHROPIC_API_KEY"), resolved at setup. Never pinned or logged. */
  readonly apiKey?: string | Secret;
  readonly baseURL?: string;
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

export function anthropic(options: AnthropicOptions): Model {
  const params = options.params ?? {};
  const clash = RESERVED.find((key) => Object.hasOwn(params, key));
  if (clash !== undefined)
    throw new ConfigError(
      "invalid_config",
      `anthropic params can't set ${clash}: the adapter derives it from the render`,
    );
  const hosted = options.hostedTools ?? [];
  checkHostedTools("anthropic", hosted, HOSTED_READ_ONLY);
  const info: ModelInfo = {
    model: { provider: "anthropic", name: options.model },
    adapter: {
      ...ADAPTER,
      settings: hosted.length === 0 ? {} : { hosted_tools: [...hosted] },
    },
    params: { ...params, max_tokens: options.maxTokens },
    limits: {
      provider: "anthropic",
      name: options.model,
      context_window: options.contextWindow,
      max_output_tokens: options.maxOutputTokens,
      input_billing_bound: "context_window",
      ...(options.price === undefined ? {} : { price: options.price }),
    },
    accepts: ["text", "image_ref", "document_ref"],
    hosted_tools: hosted.map((t) => String(t["name"] ?? t["type"])),
    // The Messages API has no retrieval by client request id.
    lookup: "none",
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
    ...(options.baseURL === undefined ? {} : { baseURL: options.baseURL }),
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
    for await (const chunk of decode(events, ctx)) {
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

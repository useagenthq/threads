import type {
  Fetch,
  JsonObject,
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
  modelLimits,
  parseRender,
  rejectionFor,
  staleEpoch,
} from "@threads/core/adapter";
import OpenAI, {
  APIConnectionError,
  APIError,
  APIUserAbortError,
} from "openai";
import type { Stream } from "openai/core/streaming";
import { toOpenAI } from "./request";
import { decode, ResponseFailed } from "./stream";

export type { JsonObject, Price } from "@threads/core/adapter";

// openai(): the Responses API through the official SDK, as a threads Model (spec/api.json).
// threads owns every attempt, so SDK retries are off, and the request body is
// derived from the Render v1 bytes alone.

type Rejected = Extract<ModelChunk, { kind: "rejected" }>;

/** The limits default from spec/models/openai.v1.json when the model id is listed there. */
export type OpenAIOptions = LimitOptions & {
  /** Other Responses API parameters (reasoning, text, ...), pinned in line 0. */
  readonly params?: JsonObject;
  readonly price?: Price;
  /** Provider-executed tools (web search only), sent as recorded here. */
  readonly hostedTools?: readonly JsonObject[];
  /** Defaults to secret("OPENAI_API_KEY"), resolved at setup. Never pinned or logged. */
  readonly apiKey?: string | Secret;
  readonly baseUrl?: string;
  readonly fetch?: Fetch;
};

const RESERVED = [
  "model",
  "input",
  "instructions",
  "tools",
  "stream",
  "store",
  "include",
  "previous_response_id",
  "conversation",
  "background",
  "max_tokens",
  "max_output_tokens",
];
/** The cap is set only by the maxTokens option. */
const CAPS = new Set(["max_tokens", "max_output_tokens"]);

/** Hosted tools read-only toward the outside world: web search only. */
const HOSTED_READ_ONLY = /^web_search(_preview)?(_\d{4}_\d{2}_\d{2})?$/;

/** An OpenAI model by its exact id: openai("gpt-5.5"). */
export function openai(model: string, options: OpenAIOptions = {}): Model {
  const params = options.params ?? {};
  const clash = RESERVED.find((key) => Object.hasOwn(params, key));
  if (clash !== undefined)
    throw new ConfigError(
      "invalid_config",
      CAPS.has(clash)
        ? `openai params can't set ${clash}: pass maxTokens`
        : `openai params can't set ${clash}: the adapter derives it from the render`,
    );
  const hosted = options.hostedTools ?? [];
  checkHostedTools("openai", hosted, HOSTED_READ_ONLY);
  const limits = modelLimits("openai", model, options);
  const info: ModelInfo = {
    model: { provider: "openai", name: model },
    adapter: {
      name: "openai",
      version: "1",
      settings: hosted.length === 0 ? {} : { hosted_tools: [...hosted] },
    },
    // Pinned under the provider-neutral key budgets read; sent as max_output_tokens.
    params: { ...params, max_tokens: limits.max_tokens },
    limits: {
      provider: "openai",
      name: model,
      context_window: limits.max_input_tokens,
      max_output_tokens: limits.max_output_tokens,
      input_billing_bound: "context_window",
      ...(options.price === undefined ? {} : { price: options.price }),
    },
    accepts: ["text", "image_ref", "document_ref"],
    hosted_tools: hosted.map((t) => String(t["type"])),
    // store is false, so nothing is retrievable afterwards.
    lookup: "none",
  };
  const apiKey = credential(
    "openai",
    "apiKey",
    options.apiKey,
    "OPENAI_API_KEY",
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
  options: OpenAIOptions,
  apiKey: () => string,
  request: ModelRequest,
  context: ModelContext,
  signal: AbortSignal | undefined,
): AsyncGenerator<ModelChunk, void, undefined> {
  const render = parseRender(request.body);
  const mapped = await toOpenAI(render, context);
  if (!mapped.ok) {
    // Refused before dispatch: nothing was sent.
    yield { kind: "rejected", reason: mapped.code };
    return;
  }
  const client = new OpenAI({
    apiKey: apiKey(),
    ...(options.baseUrl === undefined ? {} : { baseURL: options.baseUrl }),
    maxRetries: 0,
    fetch: fencedFetch(context, options.fetch ?? fetch),
  });
  let yielded = false;
  try {
    const events = await client.post<Stream<unknown>>("/responses", {
      body: mapped.body,
      stream: true,
      ...(signal === undefined ? {} : { signal }),
    });
    const ctx = { model: render.head.model.name, context };
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

const CODES = new Map<string, Rejected["reason"]>([
  ["rate_limit_exceeded", "rate_limited"],
  ["server_error", "server_error"],
  ["context_length_exceeded", "prompt_too_long"],
]);

/**
 * A failure before any content, as its class. A connection failure or abort is
 * not a rejection: the request may have left, so it stays unknown (rethrown).
 */
function rejection(error: unknown): Rejected | undefined {
  if (error instanceof ResponseFailed)
    return {
      kind: "rejected",
      reason: CODES.get(error.code ?? "") ?? "provider_error",
    };
  if (
    !(error instanceof APIError) ||
    error instanceof APIConnectionError ||
    error instanceof APIUserAbortError
  )
    return undefined;
  const code = typeof error.code === "string" ? error.code : "";
  if (error.status === undefined)
    return { kind: "rejected", reason: CODES.get(code) ?? "provider_error" };
  return rejectionFor(
    error.status,
    error.headers,
    {
      promptTooLong: code === "context_length_exceeded",
      retryable: code !== "insufficient_quota",
    },
    Date.now(),
  );
}

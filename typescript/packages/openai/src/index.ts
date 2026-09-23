import type {
  Fetch,
  JsonObject,
  Model,
  ModelChunk,
  ModelContext,
  ModelInfo,
  ModelRequest,
} from "@threads/core/adapter";
import {
  ConfigError,
  fencedFetch,
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

// openai(): the Responses API through the official SDK, as a threads Model (spec/api.json).
// threads owns every attempt, so SDK retries are off, and the request body is
// derived from the Render v1 bytes alone.

type Rejected = Extract<ModelChunk, { kind: "rejected" }>;

export type OpenAIOptions = {
  /** The model id the caller chooses, e.g. "gpt-5.5". No default. */
  readonly model: string;
  /** The model's declared context window and output cap. */
  readonly contextWindow: number;
  readonly maxOutputTokens: number;
  /** Responses API parameters (max_output_tokens, reasoning, text, ...), pinned in line 0. */
  readonly params?: JsonObject;
  readonly price?: ModelInfo["limits"]["price"];
  /** Provider-executed tools (web_search, file_search, ...), sent as recorded here. */
  readonly hostedTools?: readonly JsonObject[];
  /** Defaults to the SDK's OPENAI_API_KEY. Never pinned or logged. */
  readonly apiKey?: string;
  readonly baseURL?: string;
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
];

export function openai(options: OpenAIOptions): Model {
  const params = options.params ?? {};
  const clash = RESERVED.find((key) => Object.hasOwn(params, key));
  if (clash !== undefined)
    throw new ConfigError(
      "invalid_config",
      `openai params can't set ${clash}: the adapter derives it from the render`,
    );
  const hosted = options.hostedTools ?? [];
  const info: ModelInfo = {
    model: { provider: "openai", name: options.model },
    adapter: {
      name: "openai",
      version: "1",
      settings: hosted.length === 0 ? {} : { hosted_tools: [...hosted] },
    },
    params,
    limits: {
      provider: "openai",
      name: options.model,
      context_window: options.contextWindow,
      max_output_tokens: options.maxOutputTokens,
      input_billing_bound: "context_window",
      ...(options.price === undefined ? {} : { price: options.price }),
    },
    accepts: ["text", "image_ref", "document_ref"],
    hosted_tools: hosted.map((t) => String(t["type"])),
    // store is false, so nothing is retrievable afterwards.
    lookup: "none",
  };
  return {
    info,
    send: (request, context, sendOptions) =>
      send(options, request, context, sendOptions?.signal),
  };
}

async function* send(
  options: OpenAIOptions,
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
    ...(options.apiKey === undefined ? {} : { apiKey: options.apiKey }),
    ...(options.baseURL === undefined ? {} : { baseURL: options.baseURL }),
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

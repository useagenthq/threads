import { AsyncLocalStorage } from "node:async_hooks";
import {
  APICallError,
  InvalidArgumentError,
  InvalidPromptError,
  type LanguageModelV4,
  UnsupportedFunctionalityError,
} from "@ai-sdk/provider";
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
  StaleEpochError,
  staleEpoch,
} from "@threads/core/adapter";
import { z } from "zod";
import { toPrompt } from "./prompt";
import { ProviderOptions } from "./replay";
import { decode, nameOf, StreamError } from "./stream";

// aiSdk(): any AI SDK v4 language model (the long tail of providers) as a threads Model. It
// calls the spec-level doStream directly, below the AI SDK's generate/stream helpers, so there
// is no hidden retry: threads owns every attempt.
//
// The model is built by a factory that is handed threads' fetch, so every provider request goes
// through the lease check at its real send point. A send can carry
// provider-hosted tools, so a model built without that fetch is refused at setup
// (transport_fence_unsupported), never run with a weaker fence.

type Rejected = Extract<ModelChunk, { kind: "rejected" }>;
type Media = ModelInfo["accepts"][number];

/** The JSON call options line 0 pins; the prompt, tools and signal come from the render. */
const CallParams = z.strictObject({
  maxOutputTokens: z.int().positive().exactOptional(),
  temperature: z.number().exactOptional(),
  stopSequences: z.array(z.string()).exactOptional(),
  topP: z.number().exactOptional(),
  topK: z.number().exactOptional(),
  presencePenalty: z.number().exactOptional(),
  frequencyPenalty: z.number().exactOptional(),
  seed: z.int().exactOptional(),
  reasoning: z
    .enum([
      "provider-default",
      "none",
      "minimal",
      "low",
      "medium",
      "high",
      "xhigh",
    ])
    .exactOptional(),
  providerOptions: ProviderOptions.exactOptional(),
});

export type AiSdkOptions = {
  /**
   * Builds the model with threads' fetch, e.g.
   * `(fetch) => createOpenAI({ fetch })("gpt-5")`. The provider must send through it.
   */
  readonly model: (fetch: Fetch) => LanguageModelV4;
  /** The transport under the lease check. Defaults to the global fetch. */
  readonly fetch?: Fetch;
  /** The model's declared context window and output cap. */
  readonly contextWindow: number;
  readonly maxOutputTokens: number;
  /** Call options (maxOutputTokens, temperature, providerOptions, ...), pinned in line 0. */
  readonly params?: JsonObject;
  /** Input parts the model takes; others fail before dispatch. Defaults to text only. */
  readonly accepts?: readonly Media[];
  /** "context_window" only when the provider bounds billed input by it; else unknown. */
  readonly inputBillingBound?: "context_window" | "none";
  readonly price?: ModelInfo["limits"]["price"];
};

const current = new AsyncLocalStorage<ModelContext>();

/**
 * The fetch handed to the model factory: it re-checks the lease at the provider's real send
 * point, after any queueing inside doStream. Outside a threads send nothing may leave.
 */
function providerFetch(inner: Fetch): Fetch {
  return async (input, init) => {
    const context = current.getStore();
    if (context === undefined)
      throw new StaleEpochError("no threads send is in progress");
    return fencedFetch(context, inner)(input, init);
  };
}

export function aiSdk(options: AiSdkOptions): Model {
  // A JavaScript caller may pass a ready model, whose transport threads can't fence.
  const factory: unknown = options.model;
  if (typeof factory !== "function")
    throw new ConfigError(
      "transport_fence_unsupported",
      "aiSdk needs a model factory that takes threads' fetch",
    );
  const model = options.model(providerFetch(options.fetch ?? fetch));
  if (model.specificationVersion !== "v4")
    throw new ConfigError(
      "invalid_config",
      "aiSdk needs an AI SDK v4 language model",
    );
  const params = CallParams.safeParse(options.params ?? {});
  if (!params.success)
    throw new ConfigError(
      "invalid_config",
      `aiSdk params: ${z.prettifyError(params.error)}`,
    );
  const provider = nameOf(model.provider);
  const info: ModelInfo = {
    model: { provider, name: model.modelId },
    adapter: { name: "ai_sdk", version: "1", settings: {} },
    params: options.params ?? {},
    limits: {
      provider,
      name: model.modelId,
      context_window: options.contextWindow,
      max_output_tokens: options.maxOutputTokens,
      input_billing_bound: options.inputBillingBound ?? "none",
      ...(options.price === undefined ? {} : { price: options.price }),
    },
    accepts: [...(options.accepts ?? ["text"])],
    hosted_tools: [],
    // doStream has no retrieval by request id.
    lookup: "none",
  };
  return {
    info,
    send: (request, context, sendOptions) =>
      send(model, options, request, context, sendOptions?.signal),
  };
}

async function* send(
  model: LanguageModelV4,
  options: AiSdkOptions,
  request: ModelRequest,
  context: ModelContext,
  signal: AbortSignal | undefined,
): AsyncGenerator<ModelChunk, void, undefined> {
  const render = parseRender(request.body);
  const mapped = await toPrompt(render, context, options.accepts ?? ["text"]);
  if (!mapped.ok) {
    // Refused before dispatch: nothing was sent.
    yield { kind: "rejected", reason: mapped.code };
    return;
  }
  const live = await context.fence();
  if (!live.ok) {
    yield { kind: "rejected", reason: "stale_epoch" };
    return;
  }
  let yielded = false;
  try {
    const { stream } = await current.run(context, () =>
      model.doStream({
        ...CallParams.parse(render.head.params),
        prompt: mapped.prompt,
        ...(mapped.tools.length === 0 ? {} : { tools: mapped.tools }),
        ...(signal === undefined ? {} : { abortSignal: signal }),
      }),
    );
    const ctx = {
      provider: render.head.model.provider,
      model: render.head.model.name,
      context,
    };
    for await (const chunk of decode(stream, ctx)) {
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

const PROMPT_TOO_LONG =
  /prompt is too long|context.length|context window|too many tokens/i;

/**
 * A failure before any content, as its class. An error without an HTTP status
 * may have been sent, so it stays unknown (rethrown). Prompt and argument errors are raised
 * before any request is made.
 */
function rejection(thrown: unknown): Rejected | undefined {
  const error = thrown instanceof StreamError ? thrown.error : thrown;
  if (
    InvalidPromptError.isInstance(error) ||
    InvalidArgumentError.isInstance(error) ||
    UnsupportedFunctionalityError.isInstance(error)
  )
    return { kind: "rejected", reason: "provider_error" };
  if (!APICallError.isInstance(error) || error.statusCode === undefined)
    return undefined;
  return rejectionFor(
    error.statusCode,
    new Headers(error.responseHeaders),
    {
      promptTooLong: PROMPT_TOO_LONG.test(
        `${error.message} ${error.responseBody ?? ""}`,
      ),
      retryable: true,
    },
    Date.now(),
  );
}

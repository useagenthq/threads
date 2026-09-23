import type { EventOf } from "../fold/state";
import type { InputPart, OutputPart, Policy, Usage } from "../log";
import type { Result } from "../result";

// The model adapter protocol (spec/api.json types Model, ModelInfo, ModelRequest, ModelResponse,
// ModelChunk, LookupResult, LookupCapability). Wire casing: snake_case fields.

type ResponseData = EventOf<"model_response">["data"];
type AbandonData = EventOf<"model_attempt_abandoned">["data"];
type ThreadStarted = EventOf<"thread_started">["data"];

/** Declared per lookup operation: none, a lookup that never proves absence, or a final one. */
export type LookupCapability = "none" | "nonfinal" | "final";

/**
 * An adapter's answer about an operation it may have performed. Only `found` and a final
 * `not_found` settle anything; `not_found_nonfinal` and `unknown` park or abandon.
 */
export type LookupResult<T> =
  | { readonly status: "found"; readonly value: T }
  | { readonly status: "not_found" }
  | { readonly status: "not_found_nonfinal" }
  | { readonly status: "unknown"; readonly reason: string };

export type ModelInfo = {
  readonly model: ThreadStarted["model"];
  readonly adapter: ThreadStarted["adapter"];
  readonly params: ThreadStarted["model_params"];
  /** Window, output cap, billing bound and price. */
  readonly limits: NonNullable<Policy["models"]>[number];
  readonly accepts: readonly InputPart["type"][];
  readonly hosted_tools?: readonly string[];
  /** Response lookup by client request id. */
  readonly lookup: LookupCapability;
};

/** One attempt: the client request id `<branch_id>:<model_request event_id>` and the Render v1 bytes. */
export type ModelRequest = {
  readonly request_id: string;
  readonly body: Uint8Array;
};

export type ModelResponse = {
  readonly content: readonly OutputPart[];
  readonly stop_reason: ResponseData["stop_reason"];
  readonly usage: Usage;
  /** The provider's id for this request, or null when it gives none. */
  readonly provider_request_id: string | null;
};

/** One streamed item of an attempt. A rejection before any content is a chunk, never a throw. */
export type ModelChunk =
  | { readonly kind: "delta"; readonly text: string }
  | { readonly kind: "part"; readonly part: OutputPart }
  | {
      readonly kind: "done";
      readonly stop_reason: ResponseData["stop_reason"];
      readonly usage: Usage;
    }
  | {
      readonly kind: "rejected";
      readonly reason: AbandonData["reason"];
      readonly http_status?: number | undefined;
      readonly retry_after_ms?: number | undefined;
      readonly billing?: AbandonData["billing"] | undefined;
    };

/**
 * What a model adapter returns. One transport attempt per `send`; SDK retries off (* item 2). `lookup` is present when `info.lookup` is not `none`.
 */
export type Model = {
  readonly info: ModelInfo;
  readonly send: (
    request: ModelRequest,
    options?: { readonly signal?: AbortSignal },
  ) => AsyncIterable<ModelChunk>;
  readonly lookup?: (requestId: string) => Promise<LookupResult<ModelResponse>>;
  readonly countTokens?: (
    request: ModelRequest,
  ) => Promise<
    Result<
      number,
      { readonly code: "unavailable" | "timeout"; readonly message: string }
    >
  >;
};

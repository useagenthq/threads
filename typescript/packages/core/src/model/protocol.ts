import type { EventOf } from "../fold/state";
import type {
  ArtifactRef,
  BranchId,
  InputPart,
  OutputPart,
  Policy,
  Usage,
} from "../log";
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

/** A provider rejection class, recorded as model_attempt_abandoned. */
export type ProviderRejection = Extract<
  AbandonData["reason"],
  | "rate_limited"
  | "overloaded"
  | "server_error"
  | "prompt_too_long"
  | "provider_error"
>;

/**
 * Model.send's terminal error set (spec/api.json returns.errors): a provider rejection, the
 * fence refusing at the real send point, or a rendered part the adapter can't encode.
 */
export type SendError =
  | ProviderRejection
  | "stale_epoch"
  | "content_unsupported"
  | "continuation_unsupported"
  | "transport_fence_unsupported";

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
      readonly reason: SendError;
      readonly http_status?: number | undefined;
      readonly retry_after_ms?: number | undefined;
      readonly billing?: AbandonData["billing"] | undefined;
    };

/**
 * What the loop hands an adapter for one send or lookup (spec/api.json ModelContext). Bound to
 * the writer that created it: fence, read and put act for that branch and epoch only.
 */
export type ModelContext = {
  readonly branchId: BranchId;
  readonly epoch: number;
  /** Awaited at the real network send point; a failure means send nothing. */
  readonly fence: () => Promise<
    Result<void, { readonly code: "stale_epoch"; readonly message: string }>
  >;
  /** Verified bytes (sha256 and length) of an artifact a Render v1 line names. */
  readonly read: (ref: ArtifactRef) => Promise<
    Result<
      Uint8Array,
      {
        readonly code: "artifact_missing" | "artifact_corrupt";
        readonly message: string;
      }
    >
  >;
  /** Stores exact provider bytes; the ref is returned once they are durable. */
  readonly put: (data: Uint8Array, mediaType: string) => Promise<ArtifactRef>;
};

/**
 * What a model adapter returns. One transport attempt per `send`; SDK retries off (
 * item 2). `lookup` is present when `info.lookup` is not `none`.
 */
export type Model = {
  readonly info: ModelInfo;
  /**
   * Resolves credentials and checks configuration on the host, at check() or the first run.
   * Opens no connection; a throw is a ConfigError, retried on the next check() or run.
   */
  readonly setup?: () => Promise<void>;
  readonly send: (
    request: ModelRequest,
    context: ModelContext,
    options?: { readonly signal?: AbortSignal },
  ) => AsyncIterable<ModelChunk>;
  readonly lookup?: (
    requestId: string,
    context: ModelContext,
  ) => Promise<
    Result<
      LookupResult<ModelResponse>,
      { readonly code: "stale_epoch"; readonly message: string }
    >
  >;
};

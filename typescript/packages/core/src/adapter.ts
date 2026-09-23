// @threads/core/adapter: what a model adapter package builds on. Core never imports adapters.

export { ConfigError } from "./agent/errors";
export { credential, type Secret, secret } from "./agent/secret";
export { jsonSchema } from "./agent/tool";
export { assertNever } from "./assert-never";
export {
  type ChannelAdapter,
  type ChannelCapabilities,
  DeliveryOutcome,
  Inbound,
  Input,
  type RawRequest,
  type RawResponse,
  VerifiedDelivery,
} from "./channel/protocol";
export { responseText } from "./fold/state";
export { sha256Hex } from "./hash";
/** What a tool provider package (MCP) builds on: the dispatchable tool and its run context. */
export type { EffectClass, ToolSpec } from "./log";
export {
  type ArtifactRef,
  type InputPart,
  type Json,
  JsonObject,
  JsonValue,
  type KnownEvent,
  OutputPart,
  type ResultPart,
  type Usage,
} from "./log";
export type { ToolContext, ToolImpl, ToolRun } from "./loop/types";
/** The memory and knowledge provider kit: protocols, and the suites every provider passes. */
export {
  knowledgeProviderSuite,
  memoryProviderSuite,
  type Runner,
} from "./memory/conformance";
export {
  KnowledgeHit,
  MemoryHit,
  type ProviderError,
} from "./memory/protocol";
export type {
  LookupCapability,
  LookupResult,
  Model,
  ModelChunk,
  ModelContext,
  ModelInfo,
  ModelRequest,
  ModelResponse,
  ProviderRejection,
  SendError,
} from "./model";
export {
  memoryContext,
  putJson,
  readOrRefuse,
  Unsendable,
} from "./model/context";
/** Test kit: lets a model whose transport is mocked past the global model-request guard. */
export { markTestKit } from "./model/guard";
export { checkHostedTools } from "./model/hosted";
export {
  loadedTools,
  parseRender,
  type RenderLine,
  type RenderRequest,
  type ToolLine,
} from "./model/render-lines";
export {
  type Fetch,
  fencedFetch,
  rejectionFor,
  retryAfterMs,
  StaleEpochError,
  staleEpoch,
} from "./model/transport";
export { reference } from "./render/lines";
export type {
  Sandbox,
  SandboxContext,
  SandboxInfo,
  SandboxSession,
  Stale,
} from "./sandbox";
/** The remote sandbox kit a sandbox provider package builds on. */
export * from "./sandbox/remote";

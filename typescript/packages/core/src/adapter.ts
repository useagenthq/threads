// @threads/core/adapter: what a model adapter package builds on. Core never imports adapters.

export { ConfigError } from "./agent/errors";
export { assertNever } from "./assert-never";
export {
  type ArtifactRef,
  type InputPart,
  type Json,
  JsonObject,
  JsonValue,
  OutputPart,
  type ResultPart,
  type Usage,
} from "./log";
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

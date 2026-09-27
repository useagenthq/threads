// @threadsai/a2a/protocol: A2A 1.0 as bytes. No threads concepts live here — no log, no store, no
// effects — so both the exposed side and the client side parse and format with one implementation.
// The pinned protocol is spec/schema/a2a/.

export type { Answer, Streamed } from "./answer";
export {
  type AgentCapabilities,
  AgentCard,
  type AgentExtension,
  AgentInterface,
  type AgentSkill,
  SCHEME_KEYS,
  SecurityScheme,
  schemeKind,
} from "./card";
export { call, type Fetched, fetchBytes, type Sending } from "./client";
export {
  A2A_ERROR_DOMAIN,
  A2A_ERROR_NAMES,
  A2A_ERRORS,
  type A2aErrorName,
  type A2aFault,
  ERROR_INFO_TYPE,
  ErrorInfo,
  errorByCode,
  errorByReason,
  errorInfo,
  errorInfoIn,
  errorReason,
  type FoundErrorInfo,
  fault,
  httpStatus,
  jsonRpcCode,
} from "./errors";
export {
  METHODS,
  type Method,
  methodOf,
  type RpcId,
  RpcRequest,
  rpcFault,
  rpcOutcome,
  rpcResult,
} from "./jsonrpc";
export {
  fetchCard,
  IDEMPOTENT_SEND,
  MAX_CARD_BYTES,
  type PinFailure,
  type PinnedCard,
  PROVENANCE,
  pinCard,
} from "./pin";
export {
  CancelTaskRequest,
  DEFAULT_PAGE_SIZE,
  GetTaskRequest,
  ListTasksRequest,
  ListTasksResponse,
  MAX_PAGE_SIZE,
  SendMessageConfiguration,
  SendMessageRequest,
  SubscribeToTaskRequest,
} from "./requests";
export {
  MAX_FRAME_BYTES,
  type SseEvent,
  type SseFrame,
  type SseRead,
  sseBody,
  sseEvents,
} from "./sse";
export {
  Artifact,
  isFilePart,
  isInterrupted,
  isSettled,
  isTerminal,
  Message,
  Part,
  payloadOf,
  Role,
  SendMessageResponse,
  type StreamPayload,
  StreamResponse,
  streamPayload,
  Task,
  TaskArtifactUpdateEvent,
  TaskState,
  TaskStatus,
  TaskStatusUpdateEvent,
  textOf,
} from "./task";
export {
  A2A_JSON,
  A2A_VERSION,
  checkVersion,
  EXTENSIONS_HEADER,
  majorMinor,
  speaks1_0,
  VERSION_HEADER,
} from "./version";
export {
  BINDINGS,
  type Binding,
  bindingOf,
  HTTP,
  inboundPath,
  type Outbound,
  outbound,
  parseJson,
  streams,
  type Wire,
} from "./wire";

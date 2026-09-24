// @threads/core/host: what @threads/host builds on. Core never imports the host.

export type { HostRunner } from "./agent/hosted";
export { hostRunner } from "./agent/registry";
export { type RunResult, runResult, type ThreadRef } from "./agent/result";
export {
  openStore,
  type Store,
  storeConnection,
  storeOf,
  tenantStore,
} from "./agent/sqlite";
export { assertNever } from "./assert-never";
export { type EventOf, type ParkAddress, responseText } from "./fold/state";
export { sha256Hex } from "./hash";
export {
  BranchId,
  Budget,
  CallId,
  canonicalize,
  EventId,
  Int,
  JsonObject,
  JsonValue,
  type KnownEvent,
  ModelRef,
  Name,
  NonEmpty,
  PermissionMode,
  PermissionRule,
  Principal,
  type PrincipalKey,
  principalKey,
  ThreadId,
  Uuid,
} from "./log";
export { turnEvents } from "./loop/turn";
export { redactSecrets } from "./redact";
export { knownEvents } from "./reduce";
export { err, ok, type Result } from "./result";
export { collect, type Sandbox } from "./sandbox";
export { dispatched, FenceRefused, within } from "./sandbox/remote";
export type { Fence } from "./sandbox/remote/fence";
export {
  type ArtifactStore,
  type EventDraft,
  keepLease,
  LEASE_TTL_MS,
  type LogStore,
  type SqliteDriver,
  type SqlValue,
  type Writer,
} from "./store";
export { StoreError } from "./store/driver";
export { uuidv7 } from "./store/encode";
export { deleteTenant, deleteThread, sweepArtifacts } from "./store/retention";
export { parseRows } from "./store/tables";
export { cancelChildren } from "./thread/cancel";
export { type Alongside, control } from "./thread/control";
export { decide } from "./thread/decide";
export { type Thread, threadHandle } from "./thread/handle";
export { cancel, stopWhenIdle } from "./thread/settings";
export type { ChainEvent, LogError, VerifiedLog } from "./verify";

// @threads/a2a: A2A 1.0, both directions. `remote()` names a partner's agent; the protocol core is
// at @threads/a2a/protocol, and the exposed side lives in @threads/host, which serves a host agent
// from the same schemas. Runtime dependency: @threads/core and zod only — the adapter owns the
// message id, the attempt record and the exact bytes, which an SDK hides.

export {
  type A2aAuth,
  bearer,
  DEFAULT_TIMEOUT_MS,
  type Provenance,
  type Remote,
  type RemoteOptions,
  remote,
} from "./a2a";
export {
  A2A_ERRORS,
  A2A_VERSION,
  type A2aErrorName,
  type A2aFault,
  AgentCard,
  fetchCard,
  IDEMPOTENT_SEND,
  MAX_CARD_BYTES,
  type PinFailure,
  type PinnedCard,
  PROVENANCE,
  pinCard,
  Task,
  TaskState,
} from "./protocol";

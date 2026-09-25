// @threads/core/store-driver: the store's driver seam, for the in-repo Postgres package only.
// Internal: excluded from the API reference and from semver (spec/api.json choice store-sealed).

export { type Store, storeOver } from "./agent/sqlite";
export { sha256Hex } from "./hash";
export { err, ok, type Result } from "./result";
export {
  type ArtifactSink,
  type ArtifactStore,
  verified,
} from "./store/artifacts";
export {
  CommitUnknown,
  type Dialect,
  READ_ONLY,
  type SqlValue,
  type StoreDriver,
  StoreError,
  type TransactionOptions,
  type Tx,
} from "./store/driver";
export {
  type Commits,
  Fifo,
  guarded,
  openTx,
  type Statements,
} from "./store/tx";
export { type LogError, logError } from "./verify/error";

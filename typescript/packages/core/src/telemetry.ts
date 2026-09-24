import type { Store } from "./agent/sqlite";
import type { BranchId, ThreadId } from "./log";
import type { Result } from "./result";
import type { LogError } from "./verify/error";

// The telemetry protocol (spec/api.json Exporter): what host({telemetry}) calls. otel() in
// @threads/otel implements it; core only knows the shape and which store a host bound it to.

/** A branch Exporter.sync() could not read, and the head it failed at. */
export type SkippedBranch = {
  readonly branch_id: BranchId;
  readonly thread_id: ThreadId;
  readonly code: LogError["code"];
  readonly head_seq: number;
};

/** What one Exporter.sync() sent. */
export type SyncReport = {
  readonly spans: number;
  readonly possiblyLostEvents: number;
  readonly skipped: readonly SkippedBranch[];
};

export type SyncError = {
  readonly code: "collector_unavailable" | "collector_rejected";
  /** The collector's HTTP status, when it answered. */
  readonly status?: number;
  readonly message: string;
};

/** A telemetry exporter, such as otel(). */
export type Exporter = {
  readonly sync: () => Promise<Result<SyncReport, SyncError>>;
};

const BOUND = new WeakMap<Exporter, Store>();

/** host({store, telemetry}): an exporter made without a store exports the host's. */
export function bindTelemetry(exporter: Exporter, store: Store): void {
  BOUND.set(exporter, store);
}

/** The store a host bound the exporter to, if any. */
export function boundStore(exporter: Exporter): Store | undefined {
  return BOUND.get(exporter);
}

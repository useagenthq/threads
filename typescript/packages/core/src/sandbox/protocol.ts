import type { EventOf } from "../fold/state";
import type { SandboxId } from "../log";
import type { LookupCapability, LookupResult } from "../model/protocol";
import type { Result } from "../result";

// The sandbox adapter protocol (spec/api.json types Sandbox, SandboxInfo, SandboxSession,
// ExecOutput). Wire casing for data; option params are lowerCamel (api.json conventions).

/** What a snapshot records; the snapshot event's data. */
export type SnapshotData = EventOf<"snapshot">["data"];

/** An adapter's typed failure. Every code is a wire ErrorCode or an ApiErrorCode. */
export type Failure<Code extends string> = {
  readonly code: Code;
  readonly message: string;
};

export type SandboxInfo = {
  readonly provider: string;
  readonly egress: "enforced" | "unenforced";
  readonly capture_classes: readonly SnapshotData["capture_class"][];
  readonly browser: "none" | "headless";
  readonly desktop: "none" | "native" | "image";
  /** Per operation: create (Sandbox.lookup) and snapshot (Sandbox.lookupSnapshot). */
  readonly lookup: {
    readonly create: LookupCapability;
    readonly snapshot: LookupCapability;
  };
  /** unconfirmed: terminate always answers unknown, so every crash parks. */
  readonly termination: "confirmed" | "unconfirmed";
};

export type ExecOptions = {
  readonly cwd?: string;
  /** Exactly this env; nothing is inherited. */
  readonly env?: Readonly<Record<string, string>>;
  readonly timeoutMs?: number;
  readonly stdin?: Uint8Array;
  /** The call's effect key, durable in effect_begin before dispatch, so recovery can terminate it. */
  readonly processKey: string;
};

/** The complete output as byte streams, never truncated or buffered whole. */
export type ExecOutput = {
  /** Resolves once both streams end. */
  readonly exit_code: Promise<number>;
  readonly stdout: AsyncIterable<Uint8Array>;
  readonly stderr: AsyncIterable<Uint8Array>;
};

export type FileFailure = Failure<
  | "not_found"
  | "invalid_path"
  | "permission_denied"
  | "is_directory"
  | "too_large"
  | "unavailable"
>;

export type RestoreFailure = Failure<
  | "snapshot_expired"
  | "snapshot_missing"
  | "snapshot_restore_failed"
  | "snapshot_manifest_mismatch"
  | "unavailable"
>;

export type ReleaseFailure = Failure<"release_failed" | "unavailable">;

export type SandboxSession = {
  readonly id: SandboxId;
  /** A timeout kills the whole process group. */
  readonly exec: (
    command: readonly string[],
    options: ExecOptions,
  ) => Promise<
    Result<ExecOutput, Failure<"timeout" | "invalid_path" | "unavailable">>
  >;
  /** Kill the process group started with this key and confirm it is gone. */
  readonly terminate: (
    processKey: string,
  ) => Promise<
    Result<"terminated" | "already_exited" | "unknown", Failure<"unavailable">>
  >;
  readonly upload: (
    path: string,
    data: Uint8Array,
  ) => Promise<Result<void, FileFailure>>;
  readonly download: (path: string) => Promise<Result<Uint8Array, FileFailure>>;
  /** Freeze or stop tracked processes, capture, thaw. Returns once durable and restorable. */
  readonly snapshot: (
    operationKey: string,
  ) => Promise<
    Result<SnapshotData, Failure<"not_quiescent" | "unavailable" | "timeout">>
  >;
  /** Release the sandbox: ok → released, an error → release_failed (retried by gc). */
  readonly close: () => Promise<Result<void, ReleaseFailure>>;
};

/** What a sandbox adapter returns. Every create and restore carries a ledgered operation key. */
export type Sandbox = {
  readonly info: SandboxInfo;
  readonly create: (
    operationKey: string,
  ) => Promise<Result<SandboxSession, Failure<"unavailable" | "timeout">>>;
  readonly restore: (
    snapshotId: string,
    operationKey: string,
  ) => Promise<Result<SandboxSession, RestoreFailure>>;
  /** Present when info.lookup.create is not none. */
  readonly lookup?: (
    operationKey: string,
  ) => Promise<LookupResult<SandboxSession>>;
  /** Present when info.lookup.snapshot is not none. */
  readonly lookupSnapshot?: (
    operationKey: string,
  ) => Promise<LookupResult<SnapshotData>>;
  /** Reattach to a live row's sandbox by its ref, to terminate and close it after a restart. */
  readonly attach: (
    ref: string,
  ) => Promise<
    Result<
      SandboxSession,
      Failure<"not_found" | "resource_unknown" | "unavailable">
    >
  >;
  /** Release a snapshot by its ref. */
  readonly release: (
    ref: string,
  ) => Promise<Result<"released" | "already_gone", ReleaseFailure>>;
};

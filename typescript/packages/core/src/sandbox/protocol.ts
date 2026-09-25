import type { EventOf } from "../fold/state";
import type { BranchId, SandboxId } from "../log";
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

/**
 * A refused dispatch: the context's fence failed, so nothing reached the provider.
 * stale_epoch under owner authority, cleanup_claim_lost under cleanup authority.
 */
export type Stale = Failure<"stale_epoch" | "cleanup_claim_lost">;

/**
 * spec/api.json SandboxAuthority: the owner's lease at an epoch, or gc's cleanup claim on one
 * resources row (which outlives the owning branch).
 */
export type SandboxAuthority =
  | {
      readonly kind: "owner";
      readonly branch_id: BranchId;
      readonly epoch: number;
    }
  | {
      readonly kind: "cleanup";
      readonly resource_id: string;
      readonly claim: string;
    };

/**
 * spec/api.json SandboxContext: the authority an adapter re-checks at its real provider
 * dispatch point, after any queueing. A failed fence means call nothing.
 */
export type SandboxContext = {
  readonly authority: SandboxAuthority;
  readonly fence: () => Promise<Result<void, Stale>>;
};

export type SandboxSession = {
  readonly id: SandboxId;
  /** A timeout kills the whole process group. */
  readonly exec: (
    command: readonly string[],
    context: SandboxContext,
    options: ExecOptions,
  ) => Promise<
    Result<
      ExecOutput,
      Failure<"timeout" | "invalid_path" | "unavailable"> | Stale
    >
  >;
  /** Kill the process group started with this key and confirm it is gone. */
  readonly terminate: (
    processKey: string,
    context: SandboxContext,
  ) => Promise<
    Result<
      "terminated" | "already_exited" | "unknown",
      Failure<"unavailable"> | Stale
    >
  >;
  readonly upload: (
    path: string,
    data: Uint8Array,
    context: SandboxContext,
  ) => Promise<Result<void, FileFailure | Stale>>;
  readonly download: (
    path: string,
    context: SandboxContext,
  ) => Promise<Result<Uint8Array, FileFailure | Stale>>;
  /** Freeze or stop tracked processes, capture, thaw. Returns once durable and restorable. */
  readonly snapshot: (
    operationKey: string,
    context: SandboxContext,
  ) => Promise<
    Result<
      SnapshotData,
      Failure<"not_quiescent" | "unavailable" | "timeout"> | Stale
    >
  >;
  /** Release the sandbox: ok → released, an error → release_failed (retried by gc). */
  readonly close: (
    context: SandboxContext,
  ) => Promise<Result<void, ReleaseFailure | Stale>>;
} & Partial<Trees>;

/**
 * spec/api.json SandboxSession.exportTree and importTree: the optional capability to move
 * /workspace as one tar archive. Core reads an export with the tree reader (sandbox/trees.ts).
 */
export type Trees = {
  /** The archive on stdout, then exit code 0; any other code is a failed export. */
  readonly exportTree: (
    context: SandboxContext,
  ) => Promise<Result<ExecOutput, Failure<"unavailable"> | Stale>>;
  /** Extracts a host-built archive (modes already masked to 0o777) into /workspace, owner dropped. */
  readonly importTree: (
    tar: AsyncIterable<Uint8Array>,
    context: SandboxContext,
  ) => Promise<Result<void, Failure<"unavailable"> | Stale>>;
};

/** What a sandbox adapter returns. Every create and restore carries a ledgered operation key. */
export type Sandbox = {
  readonly info: SandboxInfo;
  /**
   * Resolves credentials and checks configuration on the host, at check() or the first run.
   * Opens no connection; a throw is a ConfigError, retried on the next check() or run.
   */
  readonly setup?: () => Promise<void>;
  readonly create: (
    operationKey: string,
    context: SandboxContext,
  ) => Promise<
    Result<SandboxSession, Failure<"unavailable" | "timeout"> | Stale>
  >;
  /**
   * Restores into a new isolated sandbox and verifies its tree against `manifestHash` (the
   * snapshot event's); a mismatch releases what it created and is snapshot_manifest_mismatch.
   */
  readonly restore: (
    snapshotId: string,
    manifestHash: string,
    operationKey: string,
    context: SandboxContext,
  ) => Promise<Result<SandboxSession, RestoreFailure | Stale>>;
  /** Present when info.lookup.create is not none. */
  readonly lookup?: (
    operationKey: string,
    context: SandboxContext,
  ) => Promise<Result<LookupResult<SandboxSession>, Stale>>;
  /** Present when info.lookup.snapshot is not none. */
  readonly lookupSnapshot?: (
    operationKey: string,
    context: SandboxContext,
  ) => Promise<Result<LookupResult<SnapshotData>, Stale>>;
  /** Reattach to a live row's sandbox by its ref, to terminate and close it after a restart. */
  readonly attach: (
    ref: string,
    context: SandboxContext,
  ) => Promise<
    Result<
      SandboxSession,
      Failure<"not_found" | "resource_unknown" | "unavailable"> | Stale
    >
  >;
  /** Release a snapshot by its ref. */
  readonly release: (
    ref: string,
    context: SandboxContext,
  ) => Promise<Result<"released" | "already_gone", ReleaseFailure | Stale>>;
};

import type { LookupResult } from "../../model/protocol";
import type { Sandbox } from "../protocol";

// What a provider package implements to become a threads Sandbox through remoteSandbox(): the
// provider's raw operations over its official SDK. Every method runs inside `within(context)`,
// sends only through a fenced transport, and throws on a transport or provider failure.
// Termination and quiescence are never inferred from inside the guest: only a
// provider control-plane primitive (a whole-sandbox pause or kill) proves them.

export type Sinks = {
  readonly stdout: (chunk: Uint8Array) => void;
  readonly stderr: (chunk: Uint8Array) => void;
};

/** A started process: `exit` resolves once both output streams have ended. */
export type Started = { readonly exit: Promise<number> };

/**
 * How the provider makes a capture quiescent, declared per provider and mode
 * from its documentation, never broader:
 * - paused: a provider-owned pause of the whole sandbox freezes every process for the capture.
 * - stopped: the provider stops the whole sandbox for it; its processes end, so a snapshot is
 *   disruptive to the parent.
 * - unconfirmed: no documented whole-sandbox boundary; the snapshot capability is absent.
 */
export type Quiescence = "paused" | "stopped" | "unconfirmed";

export type Capture = {
  readonly quiescence: Quiescence;
  /**
   * Captures the filesystem into a durable snapshot named by `operationKey` behind the
   * declared boundary, and returns with the sandbox running again.
   */
  readonly take: (
    id: string,
    operationKey: string,
  ) => Promise<{ readonly ref: string; readonly expiresAt: number | null }>;
};

export type Created =
  | { readonly kind: "created"; readonly id: string }
  | {
      readonly kind: "snapshot_missing" | "snapshot_expired";
      readonly message: string;
    };

/** A lookup's answer that never proves absence: `not_found` is only a final lookup's. */
export type NonfinalLookupResult = Exclude<
  LookupResult<string>,
  { readonly status: "not_found" }
>;

/**
 * How a lost create is found by its operation key. Undeclared, a create in flight at the
 * provider may appear later, so `find` can't answer `not_found`. A driver declares
 * `createLookup: "final"` only with a `find` whose absence the provider's contract makes final.
 */
export type Finding =
  | {
      readonly createLookup?: "nonfinal";
      readonly find: (operationKey: string) => Promise<NonfinalLookupResult>;
    }
  | {
      readonly createLookup: "final";
      readonly find: (operationKey: string) => Promise<LookupResult<string>>;
    };

/**
 * How a process key's group is ended. Undeclared, `stopProcess` is best effort and every
 * termination is unknown. A driver declares `termination: "confirmed"` only with a `terminate`
 * that proves its answer, which the kit then uses instead.
 */
export type Stopping = BestEffortStop | ConfirmedTerminate;

export type BestEffortStop = {
  readonly termination?: "unconfirmed";
  /**
   * Best effort: kills the process the provider recorded under `processKey`. It proves
   * nothing: a descendant can outlive it, and the provider's process table runs in the guest.
   */
  readonly stopProcess: (id: string, processKey: string) => Promise<void>;
};

export type ConfirmedTerminate = {
  readonly termination: "confirmed";
  readonly terminate: (
    id: string,
    processKey: string,
  ) => Promise<"terminated" | "already_exited" | "unknown">;
};

export type SandboxDriver = Finding & Stopping & DriverOperations;

type DriverOperations = {
  /**
   * Creates a sandbox that `find(operationKey)` can locate (a tag, label or unique name), from
   * the snapshot `ref` when given. Nothing from the host env is passed into it.
   */
  readonly create: (
    operationKey: string,
    snapshot: string | undefined,
  ) => Promise<Created>;
  /** Whether the sandbox `id` is still live at the provider. */
  readonly exists: (id: string) => Promise<boolean>;
  readonly kill: (id: string) => Promise<"killed" | "already_gone">;
  /**
   * Starts `sh -c script` as root with an empty provider env, recorded by the provider under
   * `processKey` when given. Resolves once the provider accepted it; output reaches `sinks` as
   * it arrives.
   */
  readonly run: (
    id: string,
    script: string,
    sinks: Sinks,
    processKey?: string,
  ) => Promise<Started>;
  readonly write: (id: string, path: string, data: Uint8Array) => Promise<void>;
  readonly read: (id: string, path: string) => Promise<Uint8Array>;
  /** Absent when the provider has no snapshots. */
  readonly snapshot?: Capture;
  readonly deleteSnapshot: (
    ref: string,
  ) => Promise<"released" | "already_gone">;
};

/** When a provider resource dies on its own. null: never (a Docker container doesn't). */
export type ProviderExpiry = {
  readonly sandboxMs: number | null;
  readonly snapshotMs: number | null;
};

/**
 * A provider adapter: the Sandbox protocol plus what it declares beyond SandboxInfo: when its
 * resources expire, and how its snapshots are made quiescent ("none": no snapshots).
 */
export type ProviderSandbox = Sandbox & {
  readonly expiry: ProviderExpiry;
  readonly quiescence: Quiescence | "none";
};

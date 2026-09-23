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

export type Created =
  | { readonly kind: "created"; readonly id: string }
  | {
      readonly kind: "snapshot_missing" | "snapshot_expired";
      readonly message: string;
    };

export type SandboxDriver = {
  /**
   * Creates a sandbox that `find(operationKey)` can locate (a tag, label or unique name), from
   * the snapshot `ref` when given. Nothing from the host env is passed into it.
   */
  readonly create: (
    operationKey: string,
    snapshot: string | undefined,
  ) => Promise<Created>;
  readonly find: (operationKey: string) => Promise<LookupResult<string>>;
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
  /**
   * Best effort: kills the process the provider recorded under `processKey`. It proves
   * nothing: a descendant can outlive it, and the provider's process table runs in the guest.
   */
  readonly stopProcess: (id: string, processKey: string) => Promise<void>;
  readonly write: (id: string, path: string, data: Uint8Array) => Promise<void>;
  readonly read: (id: string, path: string) => Promise<Uint8Array>;
  /**
   * Captures the filesystem into a durable snapshot named by `operationKey`, with the whole
   * sandbox paused by the provider for the capture. Absent when the provider
   * has no such pause: the snapshot capability is then declared absent.
   */
  readonly snapshot?: (
    id: string,
    operationKey: string,
  ) => Promise<{ readonly ref: string; readonly expiresAt: number | null }>;
  readonly deleteSnapshot: (
    ref: string,
  ) => Promise<"released" | "already_gone">;
};

/** When a provider resource dies on its own. null: never. */
export type ProviderExpiry = {
  readonly sandboxMs: number;
  readonly snapshotMs: number | null;
};

/** A provider adapter: the Sandbox protocol plus the provider expiry it declares. */
export type ProviderSandbox = Sandbox & { readonly expiry: ProviderExpiry };

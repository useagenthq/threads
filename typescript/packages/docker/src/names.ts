import { sha256Hex } from "@threads/core/adapter";

// What a container and its volumes are called, and the key hash the supervisor validates.
// The name is derived from the operation key, so a second create under the same key is a 409
// the ledger reads as its own earlier one, never a duplicate sandbox.

/** The 32 lowercase hex characters a threads name is built from. */
export function shortHash(value: string): string {
  return sha256Hex(value).slice(0, 32);
}

export function containerName(operationKey: string): string {
  return `threads-${shortHash(operationKey)}`;
}

/** The label every container carries, so a lost create is found by its operation key. */
export const OPERATION_KEY = "threads.operation_key";

/** The three named volumes of a container: /run/threads, /workspace and /tmp. */
export function volumesOf(name: string): readonly string[] {
  const suffix = name.startsWith("threads-")
    ? name.slice("threads-".length)
    : name;
  return [
    `threads-exec-${suffix}`,
    `threads-ws-${suffix}`,
    `threads-tmp-${suffix}`,
  ];
}

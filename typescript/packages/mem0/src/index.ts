import type { MemoryProvider, Secret } from "@threads/core";
import { ConfigError } from "@threads/core/adapter";

// mem0() is refused at setup. The official SDK (mem0ai 3.2.0, MemoryClient)
// sends through the global fetch and axios with no transport option, pings the API from its
// constructor, and posts telemetry on its own schedule. None of that can pass the lease fence at
// the real send point, so a stale writer could still write memory. The factory
// keeps the config line valid and fails loudly at check() or the first run, never silently.

export type Mem0Options = {
  readonly apiKey: Secret;
  readonly host?: string;
};

const REFUSED =
  "mem0: the mem0ai SDK has no transport hook (global fetch and axios, a ping from its constructor, background telemetry), so its requests can't be fenced; use supermemory() or zep(), or a provider whose SDK takes a fetch";

export function mem0(_options: Mem0Options): MemoryProvider {
  const unavailable = async () =>
    ({ ok: false, error: { code: "unavailable", message: REFUSED } }) as const;
  return {
    setup: async () => {
      throw new ConfigError("transport_fence_unsupported", REFUSED);
    },
    remember: unavailable,
    recall: unavailable,
    forget: unavailable,
  };
}

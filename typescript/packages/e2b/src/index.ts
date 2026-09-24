import type { ProviderSandbox } from "@threads/core/adapter";
import { type E2bOptions, e2bOn } from "./sandbox";

export type { E2bOptions };

/** E2B sandboxes (spec/api.json e2b). What they declare, and why: sandbox.ts. */
export function e2b(options: E2bOptions = {}): ProviderSandbox {
  return e2bOn(globalThis, options);
}

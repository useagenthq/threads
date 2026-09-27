import type { ProviderSandbox } from "threadsai/adapter";
import { type E2bOptions, e2bSandbox } from "./sandbox";

export type { E2bOptions };

/** E2B sandboxes (spec/api.json e2b). What they declare, and why: sandbox.ts. */
export function e2b(options: E2bOptions = {}): ProviderSandbox {
  return e2bSandbox(options);
}

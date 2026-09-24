import {
  ConfigError,
  credential,
  type Fetch,
  type ProviderSandbox,
  remoteSandbox,
  type Secret,
} from "@threads/core/adapter";
import { e2bDriver } from "./driver";

// e2b(): E2B sandboxes, spoken to directly over E2B's REST API and each sandbox's envd
// (driver.ts), as a threads Sandbox (spec/api.json). No E2B SDK: it takes no fetch, so its
// sends couldn't be fenced.
//
// What it declares, and why:
// - Transport: every request, control plane and envd, leaves through `fetch` behind the fence
//   at its real send, one attempt each, redirects unfollowed (transport.ts). Bun and Node alike.
// - Egress: E2B enforces `allow_internet_access: false`, the default here (egress enforced).
//   `allowInternet: true` lets the sandbox reach anything (egress unenforced).
// - Create: tagged with its operation key in sandbox metadata and found by it; a list can miss
//   a create still in flight, so lookup is nonfinal. A sandbox dies at its lifetime (expiry).
//   Nothing refreshes that timeout, as in Python.
// - Termination: a kill goes through envd, E2B's process daemon inside the guest, and can't
//   prove a detached descendant is gone: unconfirmed, so a crashed exec parks.
// - Snapshots: unconfirmed quiescence (driver.ts), so none are taken; a restore from an E2B
//   snapshot or template id is verified against the manifest hash.
// - Exec output and files move as raw bytes.

export type E2bOptions = {
  /**
   * Defaults to secret("E2B_API_KEY"), resolved at setup. Used by the host only, never passed
   * into a sandbox.
   */
  readonly apiKey?: string | Secret;
  /** The template a new sandbox starts from. Defaults to "base". */
  readonly template?: string;
  /** How long a sandbox lives, in milliseconds, before E2B kills it. Defaults to one hour. */
  readonly lifetimeMs?: number;
  /** Lets the sandbox reach the internet (egress unenforced). Defaults to false. */
  readonly allowInternet?: boolean;
  /** The E2B domain, for a self-hosted or regional deployment. Defaults to E2B's. */
  readonly domain?: string;
  /** The transport under the fence. Defaults to the runtime's fetch. */
  readonly fetch?: Fetch;
};

const HOUR_MS = 3_600_000;

export function e2bSandbox(options: E2bOptions): ProviderSandbox {
  const lifetimeMs = options.lifetimeMs ?? HOUR_MS;
  if (!Number.isInteger(lifetimeMs) || lifetimeMs <= 0)
    throw new ConfigError(
      "invalid_config",
      `e2b: lifetimeMs must be a positive whole number of milliseconds, not ${lifetimeMs}`,
    );
  const internet = options.allowInternet ?? false;
  const apiKey = credential("e2b", "apiKey", options.apiKey, "E2B_API_KEY");
  const driver = e2bDriver({
    apiKey,
    // As the e2b SDKs: E2B_DOMAIN, then E2B's own.
    domain: options.domain ?? process.env["E2B_DOMAIN"] ?? "e2b.app",
    template: options.template ?? "base",
    timeoutMs: lifetimeMs,
    internet,
    fetch: options.fetch ?? ((input, init) => globalThis.fetch(input, init)),
  });
  const sandbox = remoteSandbox(
    driver,
    {
      provider: "e2b",
      egress: internet ? "unenforced" : "enforced",
      browser: "none",
      desktop: "none",
    },
    { sandboxMs: lifetimeMs, snapshotMs: null },
  );
  return {
    ...sandbox,
    setup: async () => {
      apiKey();
    },
  };
}

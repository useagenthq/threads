import {
  ConfigError,
  credential,
  type Fetch,
  type ProviderSandbox,
  remoteSandbox,
  type Secret,
} from "@threads/core/adapter";
import { e2bDriver } from "./driver";
import { fenceable, networkFetch } from "./transport";

// e2b(): E2B sandboxes through the official SDK (npm `e2b`), as a threads Sandbox (spec/api.json).
//
// What it declares, and why:
// - Transport: every SDK request is fenced at its real send (transport.ts). On Node the SDK
//   binds undici, which can't be wrapped, so that route is refused: transport_fence_unsupported.
// - Egress: E2B enforces `allowInternetAccess: false`, the default here (egress enforced).
//   `allowInternet: true` lets the sandbox reach anything (egress unenforced).
// - Create: tagged with its operation key in sandbox metadata and found by it; a list can miss
//   a create still in flight, so lookup is nonfinal. A sandbox dies at its lifetime (expiry).
// - Termination: a kill goes through envd, E2B's process daemon inside the guest, and can't
//   prove a detached descendant is gone: unconfirmed, so a crashed exec parks.
// - Snapshots: unconfirmed quiescence (driver.ts), so none are taken; a restore from an E2B
//   snapshot or template id is verified against the manifest hash.
// - Exec output: the SDK hands stdout and stderr over as decoded text, so they are re-encoded
//   as UTF-8; bytes that aren't UTF-8 arrive as U+FFFD. Files move as raw bytes.

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
  /** The transport under the fence. Defaults to the process's fetch. */
  readonly fetch?: Fetch;
};

const HOUR_MS = 3_600_000;

/** e2b() on a given JavaScript runtime (globalThis): the refusal off Bun and Deno is testable. */
export function e2bOn(runtime: object, options: E2bOptions): ProviderSandbox {
  if (!fenceable(runtime))
    throw new ConfigError(
      "transport_fence_unsupported",
      "e2b: on Node the E2B SDK sends through undici, which threads can't fence; run on Bun",
    );
  const lifetimeMs = options.lifetimeMs ?? HOUR_MS;
  const internet = options.allowInternet ?? false;
  const apiKey = credential("e2b", "apiKey", options.apiKey, "E2B_API_KEY");
  const driver = e2bDriver({
    connection: () => ({
      apiKey: apiKey(),
      ...(options.domain === undefined ? {} : { domain: options.domain }),
    }),
    template: options.template ?? "base",
    timeoutMs: lifetimeMs,
    internet,
    fetch: options.fetch ?? networkFetch,
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

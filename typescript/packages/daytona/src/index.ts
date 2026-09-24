import {
  ConfigError,
  credential,
  type Fetch,
  type ProviderSandbox,
  remoteSandbox,
  type Secret,
} from "@threads/core/adapter";
import { type Clients, clients } from "./clients";
import { daytonaDriver } from "./driver";
import type { OpenSocket } from "./logs";

// daytona(): Daytona sandboxes through Daytona's official API clients (@daytona/api-client and
// @daytona/toolbox-api-client, the transport layer of its SDK), as a threads Sandbox.
//
// What it declares, and why:
// - Transport: every control-plane and toolbox request goes through axios's fetch adapter with
//   threads' fenced fetch; the log WebSocket is fenced as it opens.
// - Egress: `networkBlockAll`, on by default (egress enforced); `allowInternet: true` lifts it
//   (unenforced).
// - Create: the sandbox is named by its operation key and found by that name; a create still
//   in flight may appear later, so lookup is nonfinal. It dies at its lifetime (expiry).
// - Termination: ending the exec's session happens in the toolbox daemon, inside the guest:
//   unconfirmed, so a crashed exec parks.
// - Snapshots: cold (quiescence "stopped"): the sandbox is stopped for the capture and started
//   again, so its running processes end: disruptive to the parent (driver.ts).
// - Exec output: exact bytes. Daytona's log stream splits stdout from stderr with in-band
//   markers it can't escape, so each stream is hex-encoded in the sandbox and decoded here.
// - Credentials: a sandbox gets no env, and no request body or URL carries the API key. Toolbox
//   requests authenticate to Daytona's proxy with it, as Daytona's SDK does; keeping it out of
//   the guest relies on the proxy and runner stripping it (README.md).

export type DaytonaOptions = {
  /**
   * Defaults to secret("DAYTONA_API_KEY"), resolved at setup. Used only to authenticate the
   * host's requests; never passed into a sandbox.
   */
  readonly apiKey?: string | Secret;
  /** Defaults to https://app.daytona.io/api. */
  readonly apiUrl?: string;
  /** The Daytona snapshot new sandboxes start from. Defaults to Daytona's default. */
  readonly snapshot?: string;
  /** The Daytona region (target) sandboxes are created in. Defaults to the organization's. */
  readonly target?: string;
  /**
   * Wall-clock lifetime in milliseconds; Daytona deletes the sandbox after it. Daytona counts
   * whole minutes, so it is rounded up. Defaults to one hour.
   */
  readonly lifetimeMs?: number;
  /**
   * Idle minutes before Daytona stops a sandbox, and minutes after that before it deletes it: a
   * backstop for a leak, never the normal cleanup (the ledger's). A positive integer; default 60.
   */
  readonly autoStopMinutes?: number;
  /** Lifts Daytona's network block (egress unenforced). Defaults to false. */
  readonly allowInternet?: boolean;
  /** The transport under the fence. Defaults to the global fetch. */
  readonly fetch?: Fetch;
  /** Opens the log WebSocket. Defaults to the runtime's WebSocket with headers. */
  readonly openSocket?: OpenSocket;
  /** How often a wait polls, and for how long. Default 1 s, 5 min. */
  readonly pollMs?: number;
  readonly waitMs?: number;
};

export type { OpenSocket } from "./logs";

const HOUR_MS = 3_600_000;

const openSocket: OpenSocket = (url, headers) =>
  new WebSocket(url, { headers: { ...headers } });

export function daytona(options: DaytonaOptions = {}): ProviderSandbox {
  const lifetimeMs = options.lifetimeMs ?? HOUR_MS;
  // Daytona reads a TTL of 0 as "no TTL": a sandbox declared expired would live on.
  if (!Number.isInteger(lifetimeMs) || lifetimeMs <= 0)
    throw new ConfigError(
      "invalid_config",
      `daytona: lifetimeMs must be a positive whole number of milliseconds, not ${lifetimeMs}`,
    );
  const autoStopMinutes = options.autoStopMinutes ?? 60;
  if (!Number.isInteger(autoStopMinutes) || autoStopMinutes <= 0)
    throw new ConfigError(
      "invalid_config",
      `daytona: autoStopMinutes must be a positive integer, not ${autoStopMinutes}`,
    );
  const blocked = !(options.allowInternet ?? false);
  const inner: Fetch = (input, init) =>
    (options.fetch ?? globalThis.fetch)(input, init);
  const apiKey = credential(
    "daytona",
    "apiKey",
    options.apiKey,
    "DAYTONA_API_KEY",
  );
  let made: Clients | undefined;
  const driver = daytonaDriver({
    clients: () => {
      made ??= clients(
        options.apiUrl ?? "https://app.daytona.io/api",
        apiKey(),
        inner,
      );
      return made;
    },
    open: options.openSocket ?? openSocket,
    snapshot: options.snapshot,
    target: options.target,
    ttlMinutes: Math.ceil(lifetimeMs / 60_000),
    autoStopMinutes,
    networkBlockAll: blocked,
    pollMs: options.pollMs ?? 1000,
    waitMs: options.waitMs ?? 300_000,
  });
  const sandbox = remoteSandbox(
    driver,
    {
      provider: "daytona",
      egress: blocked ? "enforced" : "unenforced",
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

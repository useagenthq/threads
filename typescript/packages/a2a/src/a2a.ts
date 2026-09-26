import { ConfigError, credential, type Secret } from "@threads/core/adapter";

// remote() and bearer(): what an app writes to name a partner's A2A agent. Neither does any I/O.
// The card is fetched and pinned later — when a member starts, or when a thread first calls the
// remote's tools — so a card that changes never moves a conversation already under way, and a host
// that starts with an unreachable partner still starts.

export type Provenance = "opaque" | "none";

/** Sends `Authorization: Bearer <secret>`. The only auth helper. */
export type A2aAuth = {
  readonly kind: "bearer";
  /** Resolved on the host at setup; the value never reaches the log, a prompt or a sandbox. */
  readonly reveal: () => string;
};

/**
 * `bearer(secret("PARTNER_TOKEN"))`. To rotate the token, change the variable the secret names and
 * restart the host: a secret is resolved at setup, not per request.
 */
export function bearer(value: Secret | string): A2aAuth {
  // The fallback variable is only reached when `value` is undefined, which this signature forbids.
  return {
    kind: "bearer",
    reveal: credential("bearer", "secret", value, "A2A_BEARER_TOKEN"),
  };
}

export type RemoteOptions = {
  readonly auth?: A2aAuth;
  /**
   * What one message to this remote costs us, reserved against every covering budget before it is
   * sent. Recorded as declared, never as measured: nothing here observes a partner's spend.
   */
  readonly costPerMessage?: number;
  /** `"opaque"` (the default) sends a linkable but anonymous request id and a hop count. */
  readonly provenance?: Provenance;
  /** How long one exchange may take before its outcome is in doubt. Default 120_000. */
  readonly timeoutMs?: number;
};

export type Remote = {
  /** Shares the host's agent-name space, so a clash with a local agent is a setup error. */
  readonly name: string;
  readonly cardUrl: string;
  readonly auth: A2aAuth | undefined;
  readonly costPerMessage: number;
  readonly provenance: Provenance;
  readonly timeoutMs: number;
};

export const DEFAULT_TIMEOUT_MS = 120_000;

export function remote(
  name: string,
  cardUrl: string,
  options: RemoteOptions = {},
): Remote {
  if (!/^[a-z][a-z0-9_]*$/.test(name))
    throw new ConfigError(
      "invalid_config",
      `remote name ${JSON.stringify(name)} must match [a-z][a-z0-9_]*`,
    );
  let url: URL;
  try {
    url = new URL(cardUrl);
  } catch {
    throw new ConfigError(
      "invalid_config",
      `remote ${name}: ${cardUrl} is not a URL`,
    );
  }
  if (url.protocol !== "https:")
    throw new ConfigError(
      "invalid_config",
      `remote ${name}: a card is fetched over https, not ${url.protocol}`,
    );
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0)
    throw new ConfigError(
      "invalid_config",
      `remote ${name}: timeoutMs must be a positive integer`,
    );
  const costPerMessage = options.costPerMessage ?? 0;
  if (!Number.isSafeInteger(costPerMessage) || costPerMessage < 0)
    throw new ConfigError(
      "invalid_config",
      `remote ${name}: costPerMessage must be a whole number of nanos, as usd() gives`,
    );
  return {
    name,
    cardUrl,
    auth: options.auth,
    costPerMessage,
    provenance: options.provenance ?? "opaque",
    timeoutMs,
  };
}

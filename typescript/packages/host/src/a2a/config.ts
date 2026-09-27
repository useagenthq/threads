import type { SecurityScheme } from "@threads/a2a/protocol";
import { ConfigError, usd } from "@threads/core";
import type { Budget } from "@threads/core/host";
import type { z } from "zod";
import type { HostContext, HostedAgent } from "../context";

// host({a2a}): which host agents are served as A2A agents, and what a card may say about them.
// Validated at ready(), beside the channel and schedule checks, because every refusal here names
// a fix the operator makes in the config, never something a caller can trigger.

export type A2aExposure = {
  /**
   * Required: the card requires a description, and an agent's instructions must never be
   * published. Instructions, tool names and model names never appear in the card.
   */
  readonly description: string;
  /** What one exposed task may spend. Default: $1.00 and ten minutes of wall clock. */
  readonly budget?: z.infer<typeof Budget>;
};

export type A2aOptions = {
  readonly expose: Readonly<Record<string, A2aExposure>>;
  /**
   * Where partners reach this host, as a scheme and authority: `https://agents.acme.example`. The
   * card's interface URLs are built from it, and it is required because there is no safe way to
   * guess it. A fetch server's `request.url` comes from the request line and the `Host` header, so
   * behind a reverse proxy — the normal deployment — a card derived from a request advertises the
   * internal origin and no partner can reach us; pointed at an attacker, it is an origin a caller
   * chooses for a document whose whole job is to say where to send work.
   */
  readonly baseUrl: string;
  /**
   * Replaces the default `{bearer: {httpAuthSecurityScheme: {scheme: "bearer"}}}` for OAuth2,
   * OpenID Connect, an API key or mTLS. The card only declares the scheme; the host's
   * `authenticate` is what checks a caller.
   */
  readonly securitySchemes?: Readonly<Record<string, SecurityScheme>>;
};

/** spec/api.json: stated here, not only in code, because a card's reader cannot see it. */
export const DEFAULT_BUDGET: z.infer<typeof Budget> = {
  max_cost_nanos: usd(1),
  max_wall_ms: 600_000,
};

export type ExposedAgent = {
  readonly hosted: HostedAgent;
  readonly description: string;
  readonly budget: z.infer<typeof Budget>;
  /** The agent's pinned config hash: the card's version, so a card changes when the agent does. */
  readonly configHash: string;
};

export type Exposed = {
  readonly agents: ReadonlyMap<string, ExposedAgent>;
  readonly securitySchemes: Readonly<Record<string, SecurityScheme>>;
  /** The checked `baseUrl`, as its origin: what every card's interface URLs are built from. */
  readonly baseUrl: string;
};

const DEFAULT_SCHEMES: Readonly<Record<string, SecurityScheme>> = {
  bearer: { httpAuthSecurityScheme: { scheme: "bearer" } },
};

/**
 * The exposed map, or the ConfigError that says what to change. Reads each agent's pinned tool
 * specs, so a deferred or MCP tool counts as much as one written inline.
 */
export async function exposeA2a(
  ctx: HostContext,
  options: A2aOptions,
): Promise<Exposed> {
  // Checked rather than trusted to the type: a host's options can come from JavaScript.
  if (typeof options.baseUrl !== "string" || options.baseUrl === "")
    throw new ConfigError(
      "invalid_config",
      "a2a.baseUrl is required: a card says where a partner sends work, and that cannot be read off a request",
    );
  const agents = new Map<string, ExposedAgent>();
  for (const [name, exposure] of Object.entries(options.expose)) {
    const hosted = ctx.agents.get(name);
    if (hosted === undefined)
      throw new ConfigError(
        "invalid_config",
        `a2a.expose.${name} names no host agent`,
      );
    agents.set(name, await checked(name, hosted, exposure));
  }
  return {
    agents,
    securitySchemes: options.securitySchemes ?? DEFAULT_SCHEMES,
    baseUrl: baseOrigin(options.baseUrl),
  };
}

/**
 * The origin partners reach us at. A path, a query or a fragment is refused rather than dropped:
 * the card builder appends `/a2a/<name>` to this, so a base URL carrying a path would publish
 * endpoints the operator did not write.
 */
function baseOrigin(baseUrl: string): string {
  let parsed: URL;
  try {
    parsed = new URL(baseUrl);
  } catch {
    throw new ConfigError(
      "invalid_config",
      `a2a.baseUrl is not a URL: ${baseUrl}. Give the scheme and host partners reach this host at, like https://agents.acme.example`,
    );
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:")
    throw new ConfigError(
      "invalid_config",
      `a2a.baseUrl is ${parsed.protocol}, and a card names an http or https endpoint`,
    );
  if (parsed.pathname !== "/" || parsed.search !== "" || parsed.hash !== "")
    throw new ConfigError(
      "invalid_config",
      `a2a.baseUrl is a scheme and host only, with no path: ${baseUrl}`,
    );
  return parsed.origin;
}

async function checked(
  name: string,
  hosted: HostedAgent,
  exposure: A2aExposure,
): Promise<ExposedAgent> {
  if (hosted.runner.targets.length > 0)
    throw new ConfigError(
      "invalid_config",
      `a2a.expose.${name}: ${name} has handoffs and can't be exposed over A2A; a handoff moves the conversation to another thread, which a remote task can't follow`,
    );
  // The pin an exposed run starts with: ask_user is pinned, because the caller answers questions.
  const pin = (await hosted.runner.started({ answerer: true })).event;
  if (pin.type !== "thread_started")
    throw new Error("a new thread's pin is a thread_started");
  const acts = pin.data.tools.some((t) => t.effect_class !== "read_only");
  if (acts && hosted.runner.approvers === undefined)
    throw new ConfigError(
      "invalid_config",
      `a2a.expose.${name}: ${name} is exposed over A2A and has actions that can need approval; add approvers to agent '${hosted.name}'`,
    );
  return {
    hosted,
    description: exposure.description,
    budget: exposure.budget ?? DEFAULT_BUDGET,
    configHash: pin.data.config_hash,
  };
}

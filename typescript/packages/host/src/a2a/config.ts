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
  };
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

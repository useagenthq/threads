import { redactSecrets } from "../redact";
import { ConfigError } from "./errors";
import type { Extension } from "./extension";
import { setupOf } from "./registry";
import type { Tool } from "./tool";

// Setup: what check() or the first run resolves before anything is pinned: extension setups and
// each adapter's setup (credentials, configuration), for this agent and every agent it may
// start. A setup is remembered per object on success only, so a failure is retried by the next
// check() or run. MCP is never part of it: each check() and each run opens its own sessions.

/** One MCP connection's tools, bound to that connection until the session closes. */
export type McpSession = {
  readonly tools: readonly Tool<unknown, unknown, unknown>[];
  readonly close: () => Promise<void>;
  readonly [Symbol.asyncDispose]: () => Promise<void>;
};

/**
 * spec/api.json McpServer: an MCP server binding, built by mcp() in @threads/mcp. Core opens a
 * session for each check() and each run and pins the tools it lists, named mcp__<name>__<tool>.
 */
export type McpServer = {
  readonly kind: "mcp";
  readonly name: string;
  /** Connects, lists and filters the tools. An unreachable server throws mcp_unreachable. */
  readonly connect: () => Promise<McpSession>;
};

/** The optional capability of a Model, Sandbox, MemoryProvider or KnowledgeProvider. */
type SetsUp = { readonly setup?: () => Promise<void> };

/** Objects whose setup succeeded in this process, held weakly. */
const ready = new WeakSet<object>();
/** Setups in flight: a concurrent check() or run waits for the same attempt and its outcome. */
const running = new WeakMap<object, Promise<void>>();

async function once(target: object, setup: () => Promise<void>): Promise<void> {
  if (ready.has(target)) return;
  const known = running.get(target);
  if (known !== undefined) return known;
  // Registered before setup starts, so even a setup that throws at once is shared and cleared.
  const { promise, resolve, reject } = Promise.withResolvers<void>();
  running.set(target, promise);
  try {
    await setup();
    ready.add(target);
    resolve();
  } catch (error) {
    reject(error);
  } finally {
    // Success is in `ready`; a failure is forgotten, so the next call tries again.
    running.delete(target);
  }
  return promise;
}

async function extensionSetup<Deps>(e: Extension<Deps>): Promise<void> {
  try {
    await e.setup?.();
  } catch (error) {
    throw new ConfigError(
      "invalid_config",
      `extension ${e.name}: setup failed: ${redactSecrets(String(error))}`,
    );
  }
}

/**
 * Sets up the extensions, then the adapters (models, sandbox, memory, knowledge) in order, then
 * the agents this one may start. Each object once, however many agents share it.
 */
export async function setUp<Deps>(
  extensions: readonly Extension<Deps>[],
  adapters: readonly (SetsUp | undefined)[],
  agents: readonly object[],
): Promise<void> {
  for (const e of extensions) await once(e, () => extensionSetup(e));
  for (const a of adapters)
    if (a?.setup !== undefined) await once(a, async () => a.setup?.());
  for (const agent of agents) await setupOf(agent)?.();
}

/** One session per server, closed together; a failed connect closes those already open. */
export async function connectAll(
  servers: readonly McpServer[],
): Promise<McpSession> {
  const names = servers.map((s) => s.name);
  const twice = names.find((n, i) => names.indexOf(n) !== i);
  if (twice !== undefined)
    throw new ConfigError(
      "duplicate_name",
      `two MCP servers are named ${twice}`,
    );
  const opened = await Promise.allSettled(servers.map((s) => s.connect()));
  const sessions = opened.flatMap((o) =>
    o.status === "fulfilled" ? [o.value] : [],
  );
  const close = async (): Promise<void> => {
    await Promise.all(sessions.map((s) => s.close()));
  };
  const failed = opened.find((o) => o.status === "rejected");
  if (failed !== undefined) {
    await close();
    throw failed.reason;
  }
  return {
    tools: sessions.flatMap((s) => s.tools),
    close,
    [Symbol.asyncDispose]: close,
  };
}

export function isMcp<Deps>(
  t: Tool<unknown, unknown, Deps> | McpServer,
): t is McpServer {
  return "kind" in t && t.kind === "mcp";
}

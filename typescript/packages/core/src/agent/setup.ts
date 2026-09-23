import type { KnowledgeProvider, MemoryProvider } from "../memory/protocol";
import { ConfigError } from "./errors";
import type { Extension } from "./extension";
import type { Tool } from "./tool";

// Setup: what check() or the first run resolves before anything is pinned.
// Extension setups, provider setup checks, and MCP handshakes, each once per agent. A failure
// is a ConfigError; the agent never starts with part of its config silently missing.

/**
 * spec/api.json McpServer: an MCP server binding, built by mcp() in @threads/mcp. Core only
 * connects it at setup and pins the tools it lists, named mcp__<name>__<tool>.
 */
export type McpServer = {
  readonly kind: "mcp";
  readonly name: string;
  /** Connects, lists and filters the tools. An unreachable server throws mcp_unreachable. */
  readonly connect: () => Promise<readonly Tool<unknown, unknown, unknown>[]>;
};

export type Setup = () => Promise<readonly Tool<unknown, unknown, unknown>[]>;

export function once<Deps>(
  extensions: readonly Extension<Deps>[],
  servers: readonly McpServer[],
  providers: readonly (MemoryProvider | KnowledgeProvider | undefined)[],
): Setup {
  let done: Promise<readonly Tool<unknown, unknown, unknown>[]> | undefined;
  const all = async (): Promise<readonly Tool<unknown, unknown, unknown>[]> => {
    for (const e of extensions) {
      try {
        await e.setup?.();
      } catch (error) {
        throw new ConfigError(
          "invalid_config",
          `extension ${e.name}: setup failed: ${String(error)}`,
        );
      }
    }
    for (const p of providers) await p?.setup?.();
    const names = servers.map((s) => s.name);
    const twice = names.find((n, i) => names.indexOf(n) !== i);
    if (twice !== undefined)
      throw new ConfigError(
        "duplicate_name",
        `two MCP servers are named ${twice}`,
      );
    const tools = await Promise.all(servers.map((s) => s.connect()));
    return tools.flat();
  };
  return () => {
    done ??= all();
    return done;
  };
}

export function isMcp<Deps>(
  t: Tool<unknown, unknown, Deps> | McpServer,
): t is McpServer {
  return "kind" in t && t.kind === "mcp";
}

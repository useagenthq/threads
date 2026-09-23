import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import type { Tool as ServerTool } from "@modelcontextprotocol/sdk/types.js";
import type { McpServer, McpSession, Secret, Tool } from "@threads/core";
import {
  ConfigError,
  type Fetch,
  sandboxFetch,
  within,
} from "@threads/core/adapter";
import { type Effect, serverTools } from "./tools";
import { clientTransport } from "./transport";

// mcp() (spec/api.json): an MCP server in one line, by URL
// (Streamable HTTP, SSE as the legacy fallback) or by stdio command. The host owns the
// connection and the credentials: secret() values are resolved here, on the host, and never
// reach the log, a prompt or the sandbox. Each check() and each run opens its own session: its
// tools are listed, filtered and pinned, and call through that session's connection until it
// closes. An unreachable server is a setup error naming it, never silently missing tools.

export type McpOptions = {
  /** Tools are named mcp__<name>__<tool>. */
  readonly name: string;
  readonly url?: string;
  readonly headers?: Readonly<Record<string, string | Secret>>;
  readonly command?: string;
  readonly args?: readonly string[];
  readonly env?: Readonly<Record<string, string | Secret>>;
  /** "sandbox" (a stdio server inside the sandbox) isn't supported yet: a setup error. */
  readonly runs?: "host" | "sandbox";
  /** Filters before pinning, by the server's own tool names. */
  readonly tools?: {
    readonly allow?: readonly string[];
    readonly deny?: readonly string[];
  };
  /**. Undeclared: unguarded, so an uncertain call parks and is never retried. */
  readonly effect?: "read_only" | "unguarded" | "idempotent";
  /** Required exactly when effect is idempotent: the server dedups on the effect key within it. */
  readonly dedupWindowMs?: number;
  /** The HTTP client's fetch (a proxy, a test server); the fence wraps it either way. */
  readonly fetch?: Fetch;
};

/** Setup runs outside any branch: its handshake and listing pass the fence. */
const SETUP = { fence: async () => ({ ok: true, value: undefined }) as const };
const HANDSHAKE_MS = 30_000;

function revealed(
  values: Readonly<Record<string, string | Secret>> | undefined,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values ?? {}).map(([k, v]) => [
      k,
      typeof v === "string" ? v : v.reveal(),
    ]),
  );
}

function effectOf(o: McpOptions): Effect {
  const wrong = new ConfigError(
    "invalid_config",
    `mcp server ${o.name}: dedupWindowMs is required exactly when effect is idempotent`,
  );
  if (o.effect === "idempotent") {
    if (o.dedupWindowMs === undefined) throw wrong;
    return { effect: "idempotent", dedupWindowMs: o.dedupWindowMs };
  }
  if (o.dedupWindowMs !== undefined) throw wrong;
  return { effect: o.effect ?? "unguarded" };
}

function base(): Record<string, string> {
  return Object.fromEntries(
    ["PATH", "HOME"].flatMap((k) => {
      const v = process.env[k];
      return v === undefined ? [] : [[k, v]];
    }),
  );
}

function transports(o: McpOptions): readonly (() => Transport)[] {
  if ((o.url === undefined) === (o.command === undefined))
    throw new ConfigError(
      "invalid_config",
      `mcp server ${o.name}: give exactly one of url or command`,
    );
  if ((o.runs ?? "host") !== "host")
    throw new ConfigError(
      "capability_missing",
      `mcp server ${o.name}: runs "sandbox" is not supported yet; stdio servers run on the host`,
    );
  if (o.url !== undefined) {
    const url = new URL(o.url);
    const requestInit = { headers: revealed(o.headers) };
    const fetch = sandboxFetch(o.fetch ?? globalThis.fetch);
    return [
      () =>
        clientTransport(
          new StreamableHTTPClientTransport(url, { requestInit, fetch }),
          false,
        ),
      () =>
        clientTransport(
          new SSEClientTransport(url, { requestInit, fetch }),
          false,
        ),
    ];
  }
  const command = o.command ?? "";
  const params = {
    command,
    args: [...(o.args ?? [])],
    // Only what a process needs to start, plus what the config gives it: no host env leaks in.
    env: { ...base(), ...revealed(o.env) },
    stderr: "ignore" as const,
  };
  return [() => clientTransport(new StdioClientTransport(params), true)];
}

function allowed(o: McpOptions, t: ServerTool): boolean {
  const { allow, deny } = o.tools ?? {};
  return (
    (allow === undefined || allow.includes(t.name)) &&
    !(deny ?? []).includes(t.name)
  );
}

async function listAll(client: Client): Promise<readonly ServerTool[]> {
  const out: ServerTool[] = [];
  let cursor: string | undefined;
  do {
    const page = await client.listTools(cursor === undefined ? {} : { cursor });
    out.push(...page.tools);
    cursor = page.nextCursor;
  } while (cursor !== undefined);
  return out;
}

/** Tries each transport in order (Streamable HTTP, then SSE); the last failure names the server. */
async function connected(o: McpOptions, client: () => Client): Promise<Client> {
  let last: unknown;
  for (const make of transports(o)) {
    const c = client();
    const done = await within(SETUP, () =>
      c.connect(make(), { timeout: HANDSHAKE_MS }),
    );
    if (done.ok) return c;
    last = done.error.error;
    await c.close();
  }
  throw new ConfigError(
    "mcp_unreachable",
    `mcp server ${o.name}: ${String(last)}`,
  );
}

/** The connected client's filtered tools, bound to it. */
async function listed(
  options: McpOptions,
  client: Client,
  effect: Effect,
): Promise<readonly Tool<unknown, unknown, unknown>[]> {
  const got = await within(SETUP, () => listAll(client));
  if (!got.ok)
    throw new ConfigError(
      "mcp_unreachable",
      `mcp server ${options.name}: tools/list failed: ${String(got.error.error)}`,
    );
  return serverTools(
    options.name,
    client,
    got.value.filter((t) => allowed(options, t)),
    effect,
  );
}

export function mcp(options: McpOptions): McpServer {
  const connect = async (): Promise<McpSession> => {
    const effect = effectOf(options);
    const client = await connected(
      options,
      () => new Client({ name: "threads", version: "0.0.0" }),
    );
    // Ends the connection (and a stdio server's process): the session's tools die with it.
    const close = async (): Promise<void> => {
      await client.close();
    };
    try {
      const tools = await listed(options, client, effect);
      return { tools, close, [Symbol.asyncDispose]: close };
    } catch (error) {
      await close();
      throw error;
    }
  };
  return { kind: "mcp", name: options.name, connect };
}

import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import {
  type CallToolResult,
  ErrorCode,
  McpError,
  type ReadResourceResult,
  type Tool as ServerTool,
} from "@modelcontextprotocol/sdk/types.js";
import type { Tool } from "@threads/core";
import {
  ConfigError,
  type Dispatched,
  dispatched,
  JsonObject,
  jsonSchema,
  reference,
  type ToolContext,
  type ToolImpl,
  type ToolRun,
  type ToolSpec,
} from "@threads/core/adapter";
import { z } from "zod";
import { argumentsSchema } from "./schema";

// A server's tools as threads tools: namespaced mcp__<server>__<tool>, the
// server's schema checked before any effect, the declared effect class (unguarded unless
// configured), and the output returned as untrusted reference. The loop owns the effect path:
// effect_begin before dispatch, and an uncertain call parks, never retried blindly (C3).

export type Effect =
  | { readonly effect: "read_only" | "unguarded" }
  | { readonly effect: "idempotent"; readonly dedupWindowMs: number };

/** The key a server may dedup on when the config declares its tools idempotent. */
const EFFECT_KEY = "threads/effect_key";
const NAME = /^[a-z][a-z0-9_]{0,63}$/;

/** mcp__<server>__<tool>, lowercased with other characters as _, or a setup error. */
export function toolName(server: string, tool: string): string {
  const name = `mcp__${server}__${tool.toLowerCase().replace(/[^a-z0-9_]/g, "_")}`;
  if (!NAME.test(name))
    throw new ConfigError(
      "invalid_config",
      `mcp server ${server}: tool ${tool} can't be named ${name} (at most 64 of [a-z0-9_])`,
    );
  return name;
}

/** A request the server hasn't answered in this long is a timeout: uncertain, never retried. */
const CALL_TIMEOUT_MS = 120_000;

type Part = CallToolResult["content"][number];

/** Text parts as they are; anything else named, not inlined (the Python host's rendering). */
const partText = (p: Part): string =>
  p.type === "text" ? p.text : `[${p.type} content omitted]`;

/** The server's content is data the model reads, never instructions: framed as reference. */
function shown(name: string, text: string, isError: boolean): ToolRun {
  return { kind: "done", output: reference("mcp", name, text), isError };
}

/** A call that didn't return: nothing sent, the server's own answer, or uncertainty (C3). */
function failed(
  name: string,
  error: unknown,
  d: Pick<Dispatched<unknown>, "sent" | "refused">,
): ToolRun {
  if (d.refused && !d.sent) return { kind: "not_sent" };
  if (error instanceof McpError)
    switch (error.code) {
      case ErrorCode.RequestTimeout:
        return { kind: "unknown", reason: "timeout" };
      case ErrorCode.ConnectionClosed:
        return { kind: "unknown", reason: "transport_error" };
      default:
        // A JSON-RPC error is the server's answer: it received the call and refused it. Its
        // text is the server's, so it is framed like any output (invariant 6).
        return shown(
          name,
          `error ${error.code}: ${error.message.replace(`MCP error ${error.code}: `, "")}`,
          true,
        );
    }
  return { kind: "unknown", reason: "transport_error" };
}

/** The call under the lease fence: its value, or the run a failure amounts to. */
async function send<T>(
  name: string,
  ctx: ToolContext,
  call: () => Promise<T>,
): Promise<
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly run: ToolRun }
> {
  const d = await dispatched(ctx, call);
  return d.outcome.ok
    ? d.outcome
    : { ok: false, run: failed(name, d.outcome.error, d) };
}

function spec(name: string, t: ServerTool, effect: Effect): ToolSpec {
  const base = {
    name,
    description: t.description ?? t.title ?? t.name,
    input_schema: JsonObject.parse(t.inputSchema),
  };
  return effect.effect === "idempotent"
    ? {
        ...base,
        effect_class: "idempotent",
        dedup_window_ms: effect.dedupWindowMs,
      }
    : { ...base, effect_class: effect.effect };
}

function asTool(
  name: string,
  spec: ToolSpec,
  impl: Omit<ToolImpl, "spec">,
): Tool<unknown, unknown, unknown> {
  return { name, spec: () => spec, bind: () => ({ spec, ...impl }) };
}

function callTool(
  server: string,
  client: Client,
  t: ServerTool,
  effect: Effect,
): Tool<unknown, unknown, unknown> {
  const name = toolName(server, t.name);
  const input = argumentsSchema(name, JsonObject.parse(t.inputSchema));
  return asTool(name, spec(name, t, effect), {
    input,
    run: async (args, ctx) => {
      const parsed = JsonObject.safeParse(args);
      if (!parsed.success)
        return {
          kind: "done",
          output: "arguments must be an object",
          isError: true,
        };
      const got = await send(name, ctx, () =>
        client.callTool(
          {
            name: t.name,
            arguments: parsed.data,
            ...(effect.effect === "idempotent"
              ? { _meta: { [EFFECT_KEY]: ctx.effectKey } }
              : {}),
          },
          undefined,
          { timeout: CALL_TIMEOUT_MS },
        ),
      );
      if (!got.ok) return got.run;
      const result: CallToolResult = { content: [], ...got.value };
      const text =
        result.content.length > 0
          ? result.content.map(partText).join("\n")
          : JSON.stringify(result.structuredContent ?? null);
      return shown(name, text, result.isError === true);
    },
  });
}

const ReadResource = z.strictObject({ uri: z.string().min(1) });

/** mcp__<server>__read_resource, when the server offers resources. */
function readResource(
  server: string,
  client: Client,
): Tool<unknown, unknown, unknown> {
  const name = toolName(server, "read_resource");
  const pinned: ToolSpec = {
    name,
    description: `Read a resource of the ${server} MCP server by URI.`,
    input_schema: jsonSchema(name, ReadResource),
    effect_class: "read_only",
  };
  return asTool(name, pinned, {
    input: ReadResource,
    run: async (args, ctx) => {
      const { uri } = ReadResource.parse(args);
      const got = await send(name, ctx, () =>
        client.readResource({ uri }, { timeout: CALL_TIMEOUT_MS }),
      );
      if (!got.ok) return got.run;
      const result: ReadResourceResult = got.value;
      const text = result.contents
        .map((c) => ("text" in c ? c.text : "[blob content omitted]"))
        .join("\n");
      return shown(name, text, false);
    },
  });
}

export function serverTools(
  server: string,
  client: Client,
  listed: readonly ServerTool[],
  effect: Effect,
): readonly Tool<unknown, unknown, unknown>[] {
  const tools = listed.map((t) => callTool(server, client, t, effect));
  if (client.getServerCapabilities()?.resources !== undefined)
    tools.push(readResource(server, client));
  const names = tools.map((t) => t.name);
  const twice = names.find((n, i) => names.indexOf(n) !== i);
  if (twice !== undefined)
    throw new ConfigError(
      "invalid_config",
      `mcp server ${server}: two tools map to ${twice}`,
    );
  return tools;
}

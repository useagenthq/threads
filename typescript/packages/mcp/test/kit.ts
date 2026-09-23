import { afterEach } from "bun:test";
import {
  type McpServer,
  type McpSession,
  openThread,
  type RunResult,
  type Tool,
  tool,
} from "@threads/core";
import type { ToolContext, ToolImpl } from "@threads/core/adapter";

type Principal = ToolContext["principal"];

import { z } from "zod";
import { type McpOptions, mcp } from "../src";
import { type Call, httpServer } from "./server";

// Shared by the MCP tests: scripted turns, servers and the sessions a test opens (closed after
// it), the public timeline, and a hand-built tool context.

const usage = { input_tokens: 10, output_tokens: 2 };
type Turn = Readonly<Record<string, unknown>>;
export const say = (text: string): Turn => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
export const use = (
  name: string,
  input: Record<string, unknown>,
  id: string,
): Turn => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});
export const URL_ = "http://mcp.test/mcp";
const sessions: McpSession[] = [];
afterEach(async () => {
  for (const s of sessions.splice(0)) await s.close();
});

/** A session the test itself opens, closed after the test. */
export async function session(h: McpServer): Promise<McpSession> {
  const s = await h.connect();
  sessions.push(s);
  return s;
}

export function server(
  options: Partial<McpOptions> & { readonly calls?: Call[] } = {},
): McpServer {
  const { calls = [], ...rest } = options;
  return mcp({ name: "docs", url: URL_, fetch: httpServer(calls), ...rest });
}

type Seen = {
  readonly type: string;
  readonly data?:
    | {
        readonly tools?: { name: string; effect_class: string }[] | undefined;
        readonly preview?: string | undefined;
        readonly origin?: string | undefined;
      }
    | undefined;
};

/** The public timeline, parsed down to what these tests read. */
const Seen: z.ZodType<Seen> = z.object({
  type: z.string(),
  data: z
    .object({
      tools: z
        .array(z.object({ name: z.string(), effect_class: z.string() }))
        .optional(),
      preview: z.string().optional(),
      origin: z.string().optional(),
    })
    .optional(),
});

export async function eventsOf<T>(result: RunResult<T>): Promise<Seen[]> {
  const thread = await openThread(result.thread.store, result.thread.id, {
    branchId: result.thread.branch,
  });
  if (!thread.ok) throw new Error(thread.error.message);
  const timeline = await thread.value.timeline();
  if (!timeline.ok) throw new Error(timeline.error.message);
  return timeline.value.entries.map((e) => Seen.parse(e.event));
}

export const note: Tool<Record<string, never>, string> = tool({
  name: "note",
  description: "Take a note.",
  input: z.object({}),
  runs: "host",
  effect: "read_only",
  execute: async () => "ok",
});

export const P: Principal = {
  issuer: "api",
  tenant: "local",
  subject: "operator",
};
export function ctx(stale = false): ToolContext {
  return {
    effectKey: "b:c1",
    callId: "c1",
    branchId: "b",
    epoch: 1,
    principal: P,
    signal: new AbortController().signal,
    fence: async () =>
      stale
        ? { ok: false, error: { code: "stale_epoch", message: "lease lost" } }
        : { ok: true, value: undefined },
  };
}

export async function bound(h: McpServer, name: string): Promise<ToolImpl> {
  const t = (await session(h)).tools.find((x) => x.name === name);
  if (t === undefined) throw new Error(`no ${name}`);
  return t.bind({
    deps: undefined,
    threadId: "t",
    branchId: "b",
    principal: P,
  });
}

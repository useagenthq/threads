import { z } from "zod";
import { type sqlite, type ThreadRef, type Tool, tool } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { loadedTools, parseRender } from "../../src/model/render-lines";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// Shared by the tool_search tests: scripted turns, deferred tools, and what a run recorded.

type Turn = Readonly<Record<string, unknown>>;
const usage = { input_tokens: 10, output_tokens: 2 };
export const say = (text: string, reads?: number): Turn => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage: reads === undefined ? usage : { ...usage, cache_read_tokens: reads },
});
export const use = (
  name: string,
  input: Record<string, unknown>,
  id: string,
  reads?: number,
): Turn => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage: reads === undefined ? usage : { ...usage, cache_read_tokens: reads },
});

export const ran: string[] = [];

type Text = { text: string };

export const lookup: Tool<{ order: string }, string> = tool({
  name: "lookup_order",
  description: "Look up an order by id.",
  input: z.object({ order: z.string() }),
  runs: "host",
  effect: "read_only",
  execute: async ({ order }) => `order ${order}: shipped`,
});

export function deferred(
  name: string,
  description: string,
): Tool<Text, string> {
  return tool({
    name,
    description,
    input: z.object({ text: z.string() }),
    runs: "host",
    effect: "unguarded",
    defer: true,
    execute: async ({ text }) => {
      ran.push(name);
      return `${name}: ${text}`;
    },
  });
}

export const createIssue: Tool<Text, string> = deferred(
  "create_issue",
  "Create a Jira issue.\nReturns its key.",
);
export const addComment: Tool<Text, string> = deferred(
  "add_comment",
  "Add a comment to a Jira issue.",
);

export type Store = ReturnType<typeof sqlite>;

export async function eventsOf(
  store: Store,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(thread.branch)));
}

/** The recorded Render v1 bytes of every turn request, in order. */
export async function requestsOf(
  store: Store,
  events: readonly KnownEvent[],
): Promise<readonly Uint8Array[]> {
  const { artifacts } = await openStore(store);
  return events.flatMap((e) =>
    e.type === "model_request" && e.data.purpose !== "compaction"
      ? [unwrap(artifacts.get(e.data.request_ref.sha256))]
      : [],
  );
}

/** The provider tools of a recorded request (Render v1, "Provider tools"). */
export const offered = (bytes: Uint8Array): readonly string[] =>
  loadedTools(parseRender(bytes)).map((t) => t.name);

export function startedOf(
  events: readonly KnownEvent[],
): Extract<KnownEvent, { type: "thread_started" }> {
  const started = events.find((e) => e.type === "thread_started");
  if (started?.type !== "thread_started") throw new Error("no thread_started");
  return started;
}

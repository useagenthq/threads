import {
  type Agent,
  agent,
  type Exporter,
  type Store,
  scriptedModel,
  tool,
} from "@threads/core";
import { storeConnection } from "@threads/core/host";
import { z } from "zod";
import { otel } from "../src";

// Scripted agents for the exporter tests: no real model anywhere.

const usage = { input_tokens: 10, output_tokens: 2 };

export const say = (text: string): unknown => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});

export const use = (
  id: string,
  input: Record<string, unknown> = {},
): unknown => ({
  content: [{ type: "tool_use", call_id: id, name: "lookup", input }],
  stop_reason: "tool_use",
  usage,
});

/**
 * An agent whose turn makes `calls` model calls: calls - 1 read-only lookups, then an answer.
 * `between` runs inside each lookup, after that model call's response is committed.
 */
export function looping(
  calls: number,
  between: () => Promise<void> = async () => {},
): Agent<never, unknown> {
  const lookup = tool({
    name: "lookup",
    description: "Look something up.",
    input: z.object({ q: z.string().optional() }),
    effect: "read_only",
    runs: "host",
    execute: async () => {
      await between();
      return "found";
    },
  });
  const responses = [
    ...Array.from({ length: calls - 1 }, (_, i) => use(`c${i + 1}`)),
    say("Done."),
  ];
  return agent({ model: scriptedModel({ responses }), tools: [lookup] });
}

export function exporter(
  store: Store,
  url: string,
  more: { content?: boolean } = {},
): Exporter {
  return otel({ store, endpoint: url, ...more });
}

/** The exporter's cursor rows, by branch. */
export async function cursors(
  store: Store,
  observer = "otel",
): Promise<Readonly<Record<string, number>>> {
  const { db } = await storeConnection(store);
  const rows = z
    .array(z.object({ branch_id: z.string(), seq: z.int() }))
    .parse(
      db.all("SELECT branch_id, seq FROM observer_cursors WHERE observer = ?", [
        observer,
      ]),
    );
  return Object.fromEntries(rows.map((r) => [r.branch_id, r.seq]));
}

/** A crash between the collector's 2xx and the cursor write: the cursor rows are gone. */
export async function forgetCursors(store: Store): Promise<void> {
  const { db } = await storeConnection(store);
  db.run("DELETE FROM observer_cursors", []);
}

import { z } from "zod";
import {
  type DynamicAgent,
  type DynamicAgentOptions,
  dynamicAgent,
  type Model,
  scriptedModel,
  type Tool,
  tool,
} from "../../src";
import type { KnownEvent } from "../../src/log";
import { call, say } from "./run-kit";

// Shared by the dynamic agent tests (lane 26): a specialist template and a lead's start call.

export const invoiceStatus: Tool<{ id: string }, string> = tool({
  name: "invoice_status",
  description: "An invoice's status.",
  input: z.object({ id: z.string() }),
  effect: "read_only",
  execute: async ({ id }) => `${id}: paid`,
});

export const readNotes: Tool<{ topic: string }, string> = tool({
  name: "read_notes",
  description: "Read the research notes.",
  input: z.object({ topic: z.string() }),
  effect: "read_only",
  execute: async ({ topic }) => `notes on ${topic}`,
});

export const PREAMBLE = "You are a careful analyst.";

/** The specialist template: two app tools and two models, fast the default. */
export function specialist(
  fast: Model = scriptedModel({ responses: [say("Paid.")] }),
  strong: Model = scriptedModel({ responses: [] }),
  more: Omit<DynamicAgentOptions<undefined, string>, "models" | "output"> = {},
): DynamicAgent {
  return dynamicAgent({
    name: "specialist",
    instructions: PREAMBLE,
    tools: [invoiceStatus, readNotes],
    ...more,
    models: { fast, strong },
  });
}

/** The lead's start of a specialist with these chosen fields. */
export const startSpecialist = (
  id: string,
  chosen: Readonly<Record<string, unknown>> = {},
): unknown =>
  call(id, "start", {
    agent: "specialist",
    task: "Is INV-1002 paid?",
    ...chosen,
  });

export const toolNames = (e: KnownEvent | undefined): readonly string[] =>
  e?.type === "thread_started" ? e.data.tools.map((t) => t.name) : [];

export const startedOf = (
  log: readonly KnownEvent[],
): Extract<KnownEvent, { type: "member_started" }>[] =>
  log.flatMap((e) => (e.type === "member_started" ? [e] : []));

export const previews = (log: readonly KnownEvent[]): readonly string[] =>
  log.flatMap((e) => (e.type === "tool_result" ? [e.data.preview] : []));

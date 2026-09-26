import { expect } from "bun:test";
import { z } from "zod";
import {
  type Extension,
  extension,
  type Hooks,
  type Tool,
  tool,
} from "../../src";
import type { KnownEvent } from "../../src/log";

// What every per-hook case shares: the scripted responses, the one echo tool, the one "ops"
// extension, and the normalization the cross-language vector compares
// (spec/conformance/vectors/hook-decisions.json).

export const usage = { input_tokens: 10, output_tokens: 2 };

export const say = (text: string): Record<string, unknown> => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});

/** One tool_use part; `uses` puts several in one response. */
export const part = (
  callId: string,
  name = "echo",
  input: Record<string, unknown> = { text: "hi" },
): Record<string, unknown> => ({
  type: "tool_use",
  call_id: callId,
  name,
  input,
});

export const uses = (
  ...parts: readonly Record<string, unknown>[]
): Record<string, unknown> => ({
  content: parts,
  stop_reason: "tool_use",
  usage,
});

export const use = (callId = "call_1"): Record<string, unknown> =>
  uses(part(callId));

export const overloaded = { error: { reason: "overloaded", http_status: 529 } };

/** The echo tool, counting how often its body ran. */
export class Box {
  runs = 0;
  readonly tool: Tool<{ text: string }, string> = tool({
    name: "echo",
    description: "Echo.",
    input: z.object({ text: z.string() }),
    runs: "host",
    effect: "read_only",
    execute: async ({ text }: { text: string }): Promise<string> => {
      this.runs += 1;
      return `echo ${text} SECRET=hunter2`;
    },
  });
}

export const EXTENSION = "ops";

/** The one extension under test; every case names it "ops", as the vector records. */
export const ops = <Deps = undefined>(hooks: Hooks<Deps>): Extension<Deps> =>
  extension<Deps>({ name: EXTENSION, hooks });

export const kinds = (log: readonly KnownEvent[]): readonly string[] =>
  log.map((e) => e.type);

/**
 * One `hook_decision` as the shared vector holds it: the hook's wire name, its decision, the
 * extension and reason, with every id field replaced by the 0-based position, in the branch's
 * log, of the event it names.
 */
export type Decision = {
  readonly hook: string;
  readonly decision: string;
  readonly extension: string;
  readonly reason?: string;
  readonly call_id?: number;
  readonly request_event_id?: number;
  readonly input_event_id?: number;
};

/** A case: it drives one public run, asserts what the hook did, and returns its decisions. */
export type HookCase = () => Promise<readonly Decision[]>;

function positions(log: readonly KnownEvent[]): {
  readonly byEvent: ReadonlyMap<string, number>;
  readonly byCall: ReadonlyMap<string, number>;
} {
  const byEvent = new Map<string, number>();
  const byCall = new Map<string, number>();
  log.forEach((event, at) => {
    byEvent.set(event.event_id, at);
    if (event.type === "tool_call") byCall.set(event.data.call_id, at);
  });
  return { byEvent, byCall };
}

function at(
  known: ReadonlyMap<string, number>,
  id: string,
  field: string,
): number {
  const found = known.get(id);
  if (found === undefined)
    throw new Error(`${field} ${id} names no event in the branch`);
  return found;
}

/** A failure's reason is the host's own error text, which each language words its own way. */
export const FAILURE = "<failure>";

/** Every `hook_decision` of a branch, normalized in log order. */
export function normalize(log: readonly KnownEvent[]): readonly Decision[] {
  const { byEvent, byCall } = positions(log);
  return log.flatMap((event) => {
    if (event.type !== "hook_decision") return [];
    const d = event.data;
    return [
      {
        hook: d.hook,
        decision: d.decision,
        extension: d.extension,
        ...(d.reason === undefined
          ? {}
          : { reason: d.decision === "failed" ? FAILURE : d.reason }),
        ...(d.call_id === undefined
          ? {}
          : { call_id: at(byCall, d.call_id, "call_id") }),
        ...(d.request_event_id === undefined
          ? {}
          : {
              request_event_id: at(
                byEvent,
                d.request_event_id,
                "request_event_id",
              ),
            }),
        ...(d.input_event_id === undefined
          ? {}
          : {
              input_event_id: at(byEvent, d.input_event_id, "input_event_id"),
            }),
      },
    ];
  });
}

/**
 * An observer changed nothing else: the run's events, minus the decisions the observer's own
 * failure added, are the events of the same run without the extension.
 */
export function onlyDecisionsDiffer(
  observed: readonly KnownEvent[],
  plain: readonly KnownEvent[],
): void {
  expect(kinds(observed).filter((k) => k !== "hook_decision")).toEqual([
    ...kinds(plain),
  ]);
}

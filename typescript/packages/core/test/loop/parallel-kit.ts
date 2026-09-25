import { z } from "zod";
import type { KnownEvent, ToolSpec } from "../../src/log";
import type {
  LoopConfig,
  LoopEnd,
  ToolContext,
  ToolImpl,
  ToolRun,
} from "../../src/loop";
import { resume } from "../../src/loop";
import type { Writer } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { events, type Harness, harness, userInput } from "./harness";

// Shared pieces for the parallel tool call tests (spec/schema/README.md, Parallel tool calls).

const usage = { input_tokens: 10, output_tokens: 2 };
export const FINAL: unknown = {
  content: [{ type: "text", text: "Done." }],
  stop_reason: "end_turn",
  usage,
};

export const read = (name: string): ToolSpec => ({
  name,
  description: `The ${name} tool.`,
  input_schema: { type: "object" },
  effect_class: "read_only",
});

export const write = (name: string): ToolSpec => ({
  ...read(name),
  effect_class: "unguarded",
});

/** One response calling `names` in order, as call_1, call_2, ... */
export function calls(...names: readonly string[]): unknown {
  return {
    content: names.map((name, i) => ({
      type: "tool_use",
      call_id: `call_${i + 1}`,
      name,
      input: {},
    })),
    stop_reason: "tool_use",
    usage,
  };
}

export const done = (output: string): ToolRun => ({
  kind: "done",
  output,
  isError: false,
});

/** A tool body; `concurrent` marks the binding like tool({concurrent: true}) does. */
export function impl(
  spec: ToolSpec,
  body: (ctx: ToolContext) => Promise<ToolRun>,
  concurrent = true,
): ToolImpl {
  return {
    spec,
    input: z.record(z.string(), z.unknown()),
    run: async (_input, ctx) => body(ctx),
    ...(concurrent ? { concurrent: true } : {}),
  };
}

export type Run = {
  readonly h: Harness;
  readonly end: LoopEnd;
  readonly log: readonly KnownEvent[];
};

/** Runs one turn on a fresh branch with these tool bodies. */
export async function run(
  impls: readonly ToolImpl[],
  responses: readonly unknown[],
  overrides: (h: Harness, writer: Writer) => Partial<LoopConfig> = () => ({}),
): Promise<Run> {
  const h = await harness(
    impls.map((i) => i.spec),
    [],
    responses,
  );
  const writer = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  const config = h.config({
    tools: new Map(impls.map((i) => [i.spec.name, i])),
    ...overrides(h, writer),
  });
  const end = await resume(writer, h.artifacts, config, {
    input: userInput("go"),
  });
  return { h, end, log: events(writer) };
}

/** Another owner takes the expired lease now. */
export async function takeOver(h: Harness): Promise<void> {
  h.clock.now += 60_000;
  unwrap(await h.store.acquire(ROOT, "usurper"));
}

export const results = (log: readonly KnownEvent[]): readonly string[] =>
  log.flatMap((e) => (e.type === "tool_result" ? [e.data.call_id] : []));

/** A promise that settles after `ms` on the real timer. */
export const after = async (ms: number): Promise<void> => {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  await promise;
};

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  type Agent,
  agent,
  type EventMatcher,
  type Extension,
  type Model,
  type SaveCaseOptions,
  type Store,
  scriptedModel,
  sqlite,
  type Thread,
  type Tool,
  tool,
} from "../../src";
import { markTestKit } from "../../src/model/guard";

// Shared pieces of the eval tests: a support agent with a read-only lookup and an effectful
// refund, scripted replies, and a fresh case directory.

export const usage: { input_tokens: number; output_tokens: number } = {
  input_tokens: 10,
  output_tokens: 2,
};

type Reply = {
  readonly content: readonly Record<string, unknown>[];
  readonly stop_reason: string;
  readonly usage: typeof usage;
};

export function say(text: string): Reply {
  return { content: [{ type: "text", text }], stop_reason: "end_turn", usage };
}

export function use(
  name: string,
  input: Record<string, unknown>,
  id: string,
): Reply {
  return {
    content: [{ type: "tool_use", call_id: id, name, input }],
    stop_reason: "tool_use",
    usage,
  };
}

/** How many refunds the tool body really issued: an eval must never add one. */
export const refunds: { count: number } = { count: 0 };

export const lookupOrder: Tool<{ id: string }, string> = tool({
  name: "lookup_order",
  description: "Look up an order by id.",
  input: z.object({ id: z.string() }),
  runs: "host",
  effect: "read_only",
  execute: async ({ id }) => `order ${id}: shipped 12 days ago`,
});

export const issueRefund: Tool<{ id: string }, string> = tool({
  name: "issue_refund",
  description: "Refund an order.",
  input: z.object({ id: z.string() }),
  runs: "host",
  effect: "unguarded",
  execute: async ({ id }) => {
    refunds.count += 1;
    return `refunded order ${id}`;
  },
});

/** The replies of one refund turn: look up, refund, answer. */
export const REFUND_TURN: readonly Reply[] = [
  use("lookup_order", { id: "42" }, "c1"),
  use("issue_refund", { id: "42" }, "c2"),
  say("Refunded order 42; it is inside the 30-day window."),
];

export function support(
  responses: readonly unknown[],
  extensions: readonly Extension[] = [],
  instructions = "You handle refunds.",
): Agent {
  return agent({
    name: "support",
    instructions,
    model: scriptedModel({ responses }),
    tools: [lookupOrder, issueRefund],
    permissions: { allow: ["lookup_order", "issue_refund"] },
    extensions,
  });
}

/** A priced scripted model: its runs have a cost. */
export function priced(
  responses: readonly unknown[],
  name = "scripted-priced",
): Model {
  const inner = scriptedModel({ responses });
  const ref = { provider: "scripted", name };
  const model = {
    ...inner,
    info: {
      ...inner.info,
      model: ref,
      limits: {
        ...inner.info.limits,
        ...ref,
        price: { input: 1000, output: 5000 },
      },
    },
  };
  markTestKit(model);
  return model;
}

/** The judge's structured answer: one verdict per criterion. */
export function verdictsReply(passes: readonly boolean[]): Reply {
  return use(
    "final_output",
    {
      verdicts: passes.map((pass, i) => ({
        criterion: i + 1,
        pass,
        reason: pass ? "It does." : "It does not.",
      })),
    },
    "v1",
  );
}

/** Runs the refund turn on a fresh thread and saves it as <dir>/<name>. */
export async function saveRefund(
  dir: string,
  name = "refund-policy",
  rubric?: readonly string[],
): Promise<string> {
  const run = await support(REFUND_TURN).run("Please refund order 42.", {
    store: memory(),
  });
  const saved = await run.thread.saveCase(name, {
    expect: { must: [{ type: "tool_call", data: { name: "lookup_order" } }] },
    externalEffects: "stub",
    ...(rubric === undefined ? {} : { rubric }),
    dir,
  });
  if (!saved.ok) throw new Error(saved.error.message);
  return saved.value.path;
}

/**
 * The simulated user's replies, in order: each is one structured UserTurn output. A string is a
 * message it sends; `{done: true}` is the user stopping. Call ids are unique per thread.
 */
export function userReplies(
  replies: readonly (
    | string
    | { readonly message?: string; readonly done: true }
    | { readonly raw: Record<string, unknown> }
  )[],
): readonly Reply[] {
  return replies.map((r, i) => {
    const output =
      typeof r === "string"
        ? { message: r, done: false }
        : "raw" in r
          ? r.raw
          : { message: r.message ?? "", done: true };
    return use("final_output", output, `u${i + 1}`);
  });
}

export type SaveOptions = Omit<
  SaveCaseOptions,
  "expect" | "externalEffects" | "dir"
> & { readonly must?: EventMatcher };

/**
 * Runs `inputs` in order on one thread and saves the last turn as <dir>/<name>. Earlier inputs
 * become the case's prefix, which is what a simulated live run continues or re-drives.
 */
export async function saveTurns(
  dir: string,
  name: string,
  target: Agent,
  inputs: readonly string[],
  options: SaveOptions = {},
): Promise<string> {
  const store = memory();
  let thread: Thread | undefined;
  for (const text of inputs) {
    const run = await target.run(
      text,
      thread === undefined ? { store } : { store, thread },
    );
    if (run.status !== "completed")
      throw new Error(`a saved turn completes: ${run.status}`);
    thread = run.thread;
  }
  if (thread === undefined) throw new Error("saveTurns needs an input");
  const { must, ...rest } = options;
  const saved = await thread.saveCase(name, {
    expect: { must: [must ?? { type: "turn_completed" }] },
    externalEffects: "stub",
    dir,
    ...rest,
  });
  if (!saved.ok) throw new Error(saved.error.message);
  return saved.value.path;
}

export function casesDir(): string {
  return mkdtempSync(join(tmpdir(), "threads-evals-"));
}

export function memory(): Store {
  return sqlite(":memory:");
}

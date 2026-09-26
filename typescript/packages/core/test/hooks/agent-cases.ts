import { expect } from "bun:test";
import { agent, type Extension, scriptedModel, sqlite } from "../../src";
import type { KnownEvent } from "../../src/log";
import { logOf, scriptedSmall } from "../agent/kit";
import {
  type Decision,
  type HookCase,
  kinds,
  normalize,
  onlyDecisionsDiffer,
  ops,
  overloaded,
  part,
  say,
  uses,
} from "./kit";

// The hooks that fire outside one plain turn: compaction, a model fallback, and a subagent.

const quick = { base_delay_ms: 1, max_delay_ms: 1 } as const;

/** A window so small the second run has to compact before its first turn request. */
const SMALL = {
  compact: {
    trigger: { tokens: 5 },
    keep_tail: { tokens: 1 },
    max_failures: 3,
  },
} as const;

/** Two runs on one thread, the second of which compacts; the branch's whole log. */
async function compacted(
  exts: readonly Extension[],
): Promise<{ readonly status: string; readonly log: readonly KnownEvent[] }> {
  const bot = agent({
    model: scriptedModel({
      responses: [say("one"), say("the user said one"), say("two")],
    }),
    context: SMALL,
    extensions: exts,
  });
  const store = sqlite(":memory:");
  const first = await bot.run("hi", { store });
  const second = await bot.run("more", { store, thread: first.thread });
  return { status: second.status, log: await logOf(second.thread) };
}

/** A turn that falls back to a second model after two overloaded answers. */
async function fellBack(
  exts: readonly Extension[],
): Promise<{ readonly status: string; readonly log: readonly KnownEvent[] }> {
  const bot = agent({
    model: scriptedModel({
      responses: [overloaded, overloaded, say("from the primary")],
    }),
    fallback: [scriptedSmall([say("from the fallback")])],
    retry: { ...quick, fallback_after: 2 },
    extensions: exts,
  });
  const result = await bot.run("go", { store: sqlite(":memory:") });
  return { status: result.status, log: await logOf(result.thread) };
}

const spawn = uses(
  part("call_1", "spawn_agent", { agent: "reviewer", prompt: "Review." }),
);

/** A lead that spawns one child, with `exts` as its extensions. */
async function spawned(
  exts: readonly Extension[],
): Promise<{ readonly status: string; readonly log: readonly KnownEvent[] }> {
  const reviewer = agent({
    name: "reviewer",
    model: scriptedModel({ responses: [say("fine")] }),
  });
  const lead = agent({
    name: "lead",
    model: scriptedModel({ responses: [spawn, say("done")] }),
    subagents: [reviewer],
    extensions: exts,
  });
  const result = await lead.run("go", { store: sqlite(":memory:") });
  return { status: result.status, log: await logOf(result.thread) };
}

export const agentCases: Record<string, HookCase> = {
  before_compact: async (): Promise<readonly Decision[]> => {
    const seen: number[] = [];
    const { status, log } = await compacted([
      ops({
        beforeCompact: async (state) => {
          seen.push(state.turns_completed);
          return { decision: "proceed" };
        },
      }),
    ]);
    expect(status).toBe("completed");
    expect(seen).toHaveLength(1);
    // The decision is durable before the summary request it gates.
    const at = kinds(log).indexOf("compacted");
    expect(kinds(log).lastIndexOf("hook_decision", at)).toBeLessThan(at);
    expect(kinds(log)).toContain("compacted");
    return normalize(log);
  },

  after_compact: async (): Promise<readonly Decision[]> => {
    const seen: number[] = [];
    const { status, log } = await compacted([
      ops({
        afterCompact: async (state) => {
          seen.push(state.turns_completed);
          return ["the invoice numbers still matter"];
        },
      }),
    ]);
    expect(status).toBe("completed");
    expect(seen).toHaveLength(1);
    // Its injections go in after the summary, before the turn's own request.
    const order = kinds(log);
    expect(order.indexOf("compacted")).toBeLessThan(order.indexOf("injected"));
    expect(order.indexOf("injected")).toBeLessThan(
      order.lastIndexOf("model_request"),
    );
    return normalize(log);
  },

  subagent_start: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, log } = await spawned([
      ops({
        subagentStart: async (call) => {
          seen.push(`${call.name}:${call.call_id}`);
          return { decision: "deny", reason: "no children today" };
        },
      }),
    ]);
    expect(status).toBe("completed");
    expect(seen).toEqual(["spawn_agent:call_1"]);
    // A denied spawn starts no child at all.
    expect(kinds(log)).not.toContain("agent_spawned");
    return normalize(log);
  },

  subagent_stop: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, log } = await spawned([
      ops({
        subagentStop: async (finished) => {
          seen.push(finished.status);
          return { decision: "stop" };
        },
      }),
    ]);
    expect(status).toBe("completed");
    expect(seen).toEqual(["completed"]);
    expect(kinds(log)).toContain("agent_finished");
    return normalize(log);
  },

  before_model_switch: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, log } = await fellBack([
      ops({
        beforeModelSwitch: async (settings) => {
          seen.push(settings.model.name);
          return { decision: "deny", reason: "stay on the pinned model" };
        },
      }),
    ]);
    expect(status).toBe("completed");
    expect(seen).toEqual(["scripted-small"]);
    // ADR 0020: a deny keeps the settings epoch and the attempt is retried on it.
    expect(kinds(log)).not.toContain("settings_changed");
    return normalize(log);
  },

  after_model_switch: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const observed = await fellBack([
      ops({
        afterModelSwitch: async (settings) => {
          seen.push(settings.model.name);
          throw new Error("dashboard down");
        },
      }),
    ]);
    const plain = await fellBack([]);
    expect(observed.status).toBe("completed");
    expect(seen).toEqual(["scripted-small"]);
    expect(kinds(observed.log)).toContain("settings_changed");
    onlyDecisionsDiffer(observed.log, plain.log);
    return normalize(observed.log);
  },
};

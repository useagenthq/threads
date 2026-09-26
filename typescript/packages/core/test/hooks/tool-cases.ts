import { expect } from "bun:test";
import { agent, type Extension, scriptedModel, sqlite } from "../../src";
import type { KnownEvent } from "../../src/log";
import { logOf } from "../agent/kit";
import {
  Box,
  type Decision,
  type HookCase,
  kinds,
  normalize,
  onlyDecisionsDiffer,
  ops,
  part,
  say,
  use,
  uses,
} from "./kit";

// The hooks around a tool call: what may run, who approves it, what the model is shown, and
// what one response's whole batch of calls leads to.

type Rules = {
  readonly allow?: readonly string[];
  readonly ask?: readonly string[];
  readonly deny?: readonly string[];
};

const ALLOW: Rules = { allow: ["echo"], ask: [], deny: [] };

/** One run of the echo turn under `rules`, with `exts` as its extensions. */
async function ran(
  responses: readonly unknown[],
  exts: readonly Extension[],
  rules: Rules = ALLOW,
): Promise<{
  readonly status: string;
  readonly runs: number;
  readonly log: readonly KnownEvent[];
}> {
  const box = new Box();
  const bot = agent({
    model: scriptedModel({ responses }),
    tools: [box.tool],
    permissions: {
      allow: [...(rules.allow ?? [])],
      ask: [...(rules.ask ?? [])],
      deny: [...(rules.deny ?? [])],
    },
    extensions: exts,
  });
  const result = await bot.run("go", { store: sqlite(":memory:") });
  return {
    status: result.status,
    runs: box.runs,
    log: await logOf(result.thread),
  };
}

const turn = [use(), say("ok")];

const resultData = (
  log: readonly KnownEvent[],
): Record<string, unknown> | undefined => {
  const found = log.find((e) => e.type === "tool_result");
  return found?.type === "tool_result" ? found.data : undefined;
};

export const toolCases: Record<string, HookCase> = {
  before_tool: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, runs, log } = await ran(turn, [
      ops({
        beforeTool: async (call) => {
          seen.push(`${call.name}:${call.call_id}`);
          return { decision: "deny", reason: "no echo today" };
        },
      }),
    ]);
    expect(status).toBe("completed");
    expect(seen).toEqual(["echo:call_1"]);
    // The deny is folded into the call's one permission_decision; nothing runs.
    expect(runs).toBe(0);
    expect(kinds(log).slice(kinds(log).indexOf("hook_decision"), -3)).toEqual([
      "hook_decision",
      "permission_decision",
      "tool_result",
    ]);
    expect(resultData(log)).toMatchObject({ preview: "denied: no echo today" });
    return normalize(log);
  },

  permission_request: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, runs, log } = await ran(
      turn,
      [
        ops({
          permissionRequest: async (call) => {
            seen.push(call.call_id);
            return { decision: "allow" };
          },
        }),
      ],
      { ask: ["echo"] },
    );
    expect(status).toBe("completed");
    expect(seen).toEqual(["call_1"]);
    // The hook answered the policy's ask, so the call ran without an approval challenge.
    expect(runs).toBe(1);
    expect(kinds(log)).not.toContain("approval_requested");
    const decided = log.find((e) => e.type === "permission_decision");
    expect(
      decided?.type === "permission_decision" ? decided.data : undefined,
    ).toMatchObject({ decision: "allow", source: "hook" });
    return normalize(log);
  },

  permission_denied: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const observed = await ran(
      turn,
      [
        ops({
          permissionDenied: async (call) => {
            seen.push(call.name);
            throw new Error("pager down");
          },
        }),
      ],
      { deny: ["echo"] },
    );
    const plain = await ran(turn, [], { deny: ["echo"] });
    expect(observed.status).toBe("completed");
    expect(seen).toEqual(["echo"]);
    expect(observed.runs).toBe(0);
    onlyDecisionsDiffer(observed.log, plain.log);
    return normalize(observed.log);
  },

  after_tool: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const observed = await ran(turn, [
      ops({
        afterTool: async (call, result) => {
          seen.push(`${call.name}:${result.origin}`);
          throw new Error("audit sink down");
        },
      }),
    ]);
    const plain = await ran(turn, []);
    expect(observed.status).toBe("completed");
    expect(seen).toEqual(["echo:executed"]);
    // The effect already happened: an observer's failure never re-runs it.
    expect(observed.runs).toBe(1);
    onlyDecisionsDiffer(observed.log, plain.log);
    return normalize(observed.log);
  },

  before_tool_result: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, log } = await ran(turn, [
      ops({
        beforeToolResult: async (call, result) => {
          seen.push(call.call_id);
          const start = Buffer.from(result.preview ?? "").indexOf("hunter2");
          return {
            decision: "redact",
            spans: [{ start, end: start + "hunter2".length }],
          };
        },
      }),
    ]);
    expect(status).toBe("completed");
    expect(seen).toEqual(["call_1"]);
    const edited = log.find((e) => e.type === "context_edited");
    expect(
      edited?.type === "context_edited" ? edited.data : undefined,
    ).toMatchObject({ reason: "guardrail" });
    return normalize(log);
  },

  after_tool_batch: async (): Promise<readonly Decision[]> => {
    const seen: number[] = [];
    const { status, runs, log } = await ran(
      [uses(part("call_1"), part("call_2")), say("ok")],
      [
        ops({
          afterToolBatch: async (state) => {
            seen.push(state.turns_completed);
            return ["remember to cite"];
          },
        }),
      ],
    );
    expect(status).toBe("completed");
    // Once, after every result of the one response is in.
    expect(seen).toHaveLength(1);
    expect(runs).toBe(2);
    const order = kinds(log);
    expect(order.lastIndexOf("tool_result")).toBeLessThan(
      order.indexOf("injected"),
    );
    expect(order.indexOf("injected")).toBeLessThan(
      order.lastIndexOf("model_request"),
    );
    return normalize(log);
  },
};

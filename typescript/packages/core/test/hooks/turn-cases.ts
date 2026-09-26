import { expect } from "bun:test";
import { z } from "zod";
import {
  agent,
  type Extension,
  openThread,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { type KnownEvent, ThreadId } from "../../src/log";
import { logOf } from "../agent/kit";
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
  use,
  uses,
} from "./kit";

// The hooks around a turn: the session, the input, each request and response, and how a turn
// ends. One public agent().run() per hook, in the situation the lane names.

const quick = { base_delay_ms: 1, max_delay_ms: 1 } as const;

/** One run with `exts` as its extensions, and its branch's log. */
async function ran(
  responses: readonly unknown[],
  exts: readonly Extension[],
  retry: Record<string, number> = {},
): Promise<{ readonly status: string; readonly log: readonly KnownEvent[] }> {
  const bot = agent({
    model: scriptedModel({ responses }),
    extensions: exts,
    ...(Object.keys(retry).length === 0 ? {} : { retry }),
  });
  const result = await bot.run("go", { store: sqlite(":memory:") });
  return { status: result.status, log: await logOf(result.thread) };
}

const OPERATOR = {
  issuer: "api",
  tenant: "local",
  subject: "operator",
} as const;

/** A run cancelled from inside a tool, so the loop ends the turn cancelled. */
async function cancelledRun(ext: Extension): Promise<string> {
  const store = sqlite(":memory:");
  const stop = tool({
    name: "stop_now",
    description: "Cancel this run.",
    input: z.object({}),
    runs: "host",
    effect: "read_only",
    execute: async (_input, ctx): Promise<string> => {
      const opened = await openThread(store, ThreadId.parse(ctx.threadId));
      if (!opened.ok) throw new Error("no thread");
      const done = await opened.value.cancel(OPERATOR);
      if (!done.ok) throw new Error("not cancelled");
      return "stopping";
    },
  });
  const bot = agent({
    model: scriptedModel({
      responses: [uses(part("call_1", "stop_now", {})), say("never")],
    }),
    tools: [stop],
    permissions: { allow: ["stop_now"], ask: [], deny: [] },
    extensions: [ext],
  });
  const result = await bot.run("go", { store, principal: OPERATOR });
  return result.status;
}

export const turnCases: Record<string, HookCase> = {
  session_start: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, log } = await ran(
      [say("ok")],
      [
        ops({
          sessionStart: async (source, ctx) => {
            seen.push(`${source}:${ctx.branchId !== ""}`);
            return ["runbook v2"];
          },
        }),
      ],
    );
    expect(status).toBe("completed");
    expect(seen).toEqual(["startup:true"]);
    // The injection is recorded with the decision, before the input it feeds.
    expect(kinds(log).slice(1, 4)).toEqual([
      "hook_decision",
      "injected",
      "user_input",
    ]);
    return normalize(log);
  },

  session_end: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const observed = await ran(
      [say("ok")],
      [
        ops({
          sessionEnd: async (ctx) => {
            seen.push(ctx.threadId);
            throw new Error("audit sink down");
          },
        }),
      ],
    );
    const plain = await ran([say("ok")], []);
    expect(observed.status).toBe("completed");
    expect(seen).toHaveLength(1);
    onlyDecisionsDiffer(observed.log, plain.log);
    // A run that ends cancelled is still a run that ended: the hook runs there too.
    const ends: string[] = [];
    const stopped = await cancelledRun(
      ops({ sessionEnd: async (ctx) => void ends.push(ctx.threadId) }),
    );
    expect(stopped).toBe("cancelled");
    expect(ends).toHaveLength(1);
    return normalize(observed.log);
  },

  before_input: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, log } = await ran(
      [say("never")],
      [
        ops({
          beforeInput: async (input) => {
            seen.push(input.text ?? "");
            return { decision: "deny", reason: "off topic" };
          },
        }),
      ],
    );
    expect(status).toBe("failed");
    expect(seen).toEqual(["go"]);
    // The input stays in the log for audit; no request is ever sent.
    expect(kinds(log)).toContain("user_input");
    expect(kinds(log)).not.toContain("model_request");
    return normalize(log);
  },

  before_model: async (): Promise<readonly Decision[]> => {
    const seen: number[] = [];
    const { status, log } = await ran(
      [say("never")],
      [
        ops({
          beforeModel: async (state) => {
            seen.push(state.turns_completed);
            return { decision: "deny", reason: "frozen window" };
          },
        }),
      ],
    );
    expect(status).toBe("failed");
    expect(seen).toEqual([0]);
    expect(kinds(log)).not.toContain("model_request");
    return normalize(log);
  },

  after_model: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const { status, log } = await ran(
      [use()],
      [
        ops({
          afterModel: async (_state, response) => {
            seen.push(response.stop_reason);
            return { decision: "deny", reason: "unsafe" };
          },
        }),
      ],
    );
    expect(status).toBe("failed");
    expect(seen).toEqual(["tool_use"]);
    // The response's call is closed denied without running, and the model sees why.
    const closed = log.find((e) => e.type === "tool_result");
    expect(
      closed?.type === "tool_result" ? closed.data : undefined,
    ).toMatchObject({ origin: "denied", preview: "denied: unsafe" });
    return normalize(log);
  },

  on_stop: async (): Promise<readonly Decision[]> => {
    const answers: readonly ("continue" | "stop")[] = ["continue", "stop"];
    let round = 0;
    const { status, log } = await ran(
      [say("first"), say("second")],
      [
        ops({
          onStop: async () => {
            const answer = answers[round++];
            return answer === "continue"
              ? { decision: "continue", reason: "keep going" }
              : { decision: "stop" };
          },
        }),
      ],
    );
    expect(status).toBe("completed");
    expect(round).toBe(2);
    // The continue is a trusted instruction, so the turn asks again.
    expect(kinds(log).filter((k) => k === "model_request")).toHaveLength(2);
    return normalize(log);
  },

  on_stop_failure: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    let stops = 0;
    const observed = await ran(
      [overloaded],
      [
        ops({
          onStop: async () => {
            stops += 1;
            return { decision: "stop" };
          },
          onStopFailure: async (code) => {
            seen.push(code);
            throw new Error("pager down");
          },
        }),
      ],
      { ...quick, max_retries: 0 },
    );
    const plain = await ran([overloaded], [], { ...quick, max_retries: 0 });
    expect(observed.status).toBe("failed");
    expect(seen).toEqual(["model_unavailable"]);
    // on_stop never runs for a turn that ends in a failure.
    expect(stops).toBe(0);
    onlyDecisionsDiffer(observed.log, plain.log);
    return normalize(observed.log);
  },

  notification: async (): Promise<readonly Decision[]> => {
    const seen: string[] = [];
    const observed = await ran(
      [overloaded, say("ok")],
      [
        ops({
          notification: async (event) => {
            seen.push(event.type);
            throw new Error("webhook timed out");
          },
        }),
      ],
      quick,
    );
    const plain = await ran([overloaded, say("ok")], [], quick);
    expect(observed.status).toBe("completed");
    // The retry wait is what a notification observer is told about.
    expect(seen).toEqual(["retry_scheduled"]);
    onlyDecisionsDiffer(observed.log, plain.log);
    return normalize(observed.log);
  },
};

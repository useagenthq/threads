import { describe, expect, test } from "bun:test";
import {
  agent,
  ConfigError,
  extension,
  type Model,
  scriptedModel,
  sqlite,
  type ThreadRef,
} from "../../src";
import type { KnownEvent } from "../../src/log";
import { logOf, scriptedSmall } from "./kit";

// Model fallback across turns (ADR 0020): with fallback_scope turn the next input reverts to the
// settings before the fallback, once, gated by before_model_switch; with scope thread it sticks.
// A thread pinned before fallback existed continues unchanged, and adding one fails closed.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const overloaded = { error: { reason: "overloaded", http_status: 529 } };

type Input = Extract<KnownEvent, { type: "user_input" }>;

/** A before_model_switch hook that answers `decision` and records what it was asked. */
function switches(decision: "allow" | "deny") {
  const asked: string[] = [];
  const gate = { decision };
  const ext = extension({
    name: "ops",
    hooks: {
      beforeModelSwitch: async (settings) => {
        asked.push(settings.model.name);
        return gate.decision === "allow"
          ? { decision: "allow" }
          : { decision: "deny", reason: "stay on this model" };
      },
    },
  });
  return { asked, gate, ext };
}

function bot(
  primary: readonly unknown[],
  fallback: Model,
  hooks: ReturnType<typeof switches>,
  scope: "turn" | "thread" = "turn",
) {
  return agent({
    name: "support",
    model: scriptedModel({ responses: primary }),
    fallback: [fallback],
    retry: {
      base_delay_ms: 1,
      max_delay_ms: 1,
      fallback_after: 3,
      fallback_scope: scope,
    },
    extensions: [hooks.ext],
  });
}

const lastInput = (log: readonly KnownEvent[]): Input => {
  const u = log.findLast((e): e is Input => e.type === "user_input");
  if (u === undefined) throw new Error("no input");
  return u;
};
const after = (log: readonly KnownEvent[], u: Input) =>
  log.filter((e) => e.seq > u.seq);
const keyed = (log: readonly KnownEvent[], u: Input) =>
  log.flatMap((e) =>
    e.type === "hook_decision" &&
    e.data.hook === "before_model_switch" &&
    e.data.input_event_id === u.event_id
      ? [e.data.decision]
      : [],
  );
const prefixes = (log: readonly KnownEvent[]) =>
  log.flatMap((e) =>
    e.type === "model_request" ? [e.data.declared_prefix.sha256] : [],
  );
const changes = (log: readonly KnownEvent[]) =>
  log.flatMap((e) => (e.type === "settings_changed" ? [e.data] : []));

async function fellBack(
  b: ReturnType<typeof bot>,
): Promise<{ readonly thread: ThreadRef }> {
  const first = await b.run("hi", { store: sqlite(":memory:") });
  expect(first).toMatchObject({
    status: "completed",
    output: "from the fallback",
  });
  return first;
}

describe("a turn-scoped fallback", () => {
  test("the next input reverts to the primary before its first request", async () => {
    const hooks = switches("allow");
    const b = bot(
      [overloaded, overloaded, overloaded, say("from the primary")],
      scriptedSmall([say("from the fallback")]),
      hooks,
    );
    const { thread } = await fellBack(b);
    const again = await b.run("again", { thread });
    expect(again).toMatchObject({
      status: "completed",
      output: "from the primary",
    });
    const log = await logOf(thread);
    const u = lastInput(log);
    const turn = after(log, u);
    expect(turn.slice(0, 3).map((e) => e.type)).toEqual([
      "hook_decision",
      "settings_changed",
      "model_request",
    ]);
    expect(changes(turn)).toMatchObject([
      {
        reason: "revert",
        cause_event_id: u.event_id,
        settings: { model: { name: "scripted-1" } },
      },
    ]);
    expect(keyed(log, u)).toEqual(["allow"]);
    // The turn's request declares the primary's line 0, as turn 1 first did.
    expect(prefixes(turn)[0]).toBe(prefixes(log)[0]);
    expect(hooks.asked).toEqual(["scripted-small", "scripted-1"]);
  });

  test("a denied revert keeps the fallback for that turn", async () => {
    const hooks = switches("allow");
    const small = scriptedSmall([
      say("from the fallback"),
      say("still the fallback"),
    ]);
    const b = bot([overloaded, overloaded, overloaded], small, hooks);
    const { thread } = await fellBack(b);
    hooks.gate.decision = "deny";
    const again = await b.run("again", { thread });
    expect(again).toMatchObject({
      status: "completed",
      output: "still the fallback",
    });
    const log = await logOf(thread);
    const u = lastInput(log);
    expect(keyed(log, u)).toEqual(["deny"]);
    expect(changes(after(log, u))).toEqual([]);
    expect(small.inner.remaining()).toBe(0);
  });

  test("with fallback_scope thread nothing reverts", async () => {
    const hooks = switches("allow");
    const b = bot(
      [overloaded, overloaded, overloaded],
      scriptedSmall([say("from the fallback"), say("still the fallback")]),
      hooks,
      "thread",
    );
    const { thread } = await fellBack(b);
    const again = await b.run("again", { thread });
    expect(again).toMatchObject({ output: "still the fallback" });
    expect(changes(await logOf(thread)).map((c) => c.reason)).toEqual([
      "fallback",
    ]);
    expect(hooks.asked).toEqual(["scripted-small"]);
  });
});

describe("setup", () => {
  test("a fallback with no bound under a budget is budget_unenforceable", async () => {
    const small = scriptedSmall([]);
    const unbounded: Model = { ...small, info: { ...small.info, params: {} } };
    const b = agent({
      model: scriptedModel({ responses: [] }),
      fallback: [unbounded],
      budget: { max_output_tokens: 10_000 },
    });
    expect(await b.check()).toMatchObject({
      ok: false,
      error: { code: "budget_unenforceable" },
    });
  });

  test("a thread pinned before fallback continues; adding one fails closed", async () => {
    // The config_hash this agent pinned before output and fallback existed.
    const PLAIN_HASH =
      "118df2e5df0d26f55795c55185e95d4b82bf0060d21b6c3653abee6bc35f1e8f";
    const plain = (fallback: readonly Model[] = []) =>
      agent({
        name: "support",
        instructions: "Help the user.",
        model: scriptedModel({ responses: [say("one"), say("two")] }),
        fallback,
      });
    const unchanged = plain();
    const first = await unchanged.run("hi", { store: sqlite(":memory:") });
    const started = (await logOf(first.thread))[0];
    expect(
      started?.type === "thread_started" ? started.data.config_hash : "",
    ).toBe(PLAIN_HASH);
    const again = await unchanged.run("more", { thread: first.thread });
    expect(again.status).toBe("completed");
    const widened = plain([scriptedSmall([])]);
    await expect(
      widened.run("more", { thread: first.thread }),
    ).rejects.toBeInstanceOf(ConfigError);
  });
});

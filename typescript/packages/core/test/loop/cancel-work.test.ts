import { describe, expect, test } from "bun:test";
import {
  agent,
  extension,
  openThread,
  scriptedModel,
  sqlite,
  type ThreadRef,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { LoopExtension } from "../../src/hooks/types";
import type { KnownEvent, Policy } from "../../src/log";
import { RETRY_DEFAULTS, resume } from "../../src/loop";
import { knownEvents } from "../../src/reduce";
import type { Writer } from "../../src/store";
import { scriptedSmall } from "../agent/kit";
import { ROOT, unwrap } from "../store/helpers";
import { cancel } from "./cancel-kit";
import { events, harness, userInput } from "./harness";

// No new work after a cancel barrier (TS): a cancel that lands while a subagent_start or a
// before_model_switch hook runs starts no child, records no model switch and schedules no retry.
// The hook's decision is still recorded; the turn ends cancelled.

const usage = { input_tokens: 10, output_tokens: 2 };
const OPERATOR = { issuer: "local", tenant: "local", subject: "operator" };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});

function afterBarrier(log: readonly KnownEvent[]): readonly string[] {
  return log
    .slice(log.findIndex((e) => e.type === "cancel_requested"))
    .map((e) => e.type);
}

describe("a cancel during subagent_start", () => {
  test("no agent_spawned, the child sends nothing, and the turn ends cancelled", async () => {
    const store = sqlite(":memory:");
    const child = scriptedModel({ responses: [say("child")] });
    const reviewer = agent({ name: "reviewer", model: child });
    let thread: ThreadRef | undefined;
    const lead = agent({
      model: scriptedModel({
        responses: [
          say("hi"),
          {
            content: [
              {
                type: "tool_use",
                call_id: "c1",
                name: "spawn_agent",
                input: { agent: "reviewer", prompt: "Review." },
              },
            ],
            stop_reason: "tool_use",
            usage,
          },
          say("never"),
        ],
      }),
      subagents: [reviewer],
      extensions: [
        extension({
          name: "gate",
          hooks: {
            subagentStart: async () => {
              if (thread !== undefined) {
                const open = await openThread(store, thread.id);
                if (!open.ok) throw new Error(open.error.message);
                const done = await open.value.cancel(OPERATOR);
                if (!done.ok) throw new Error(done.error.message);
              }
              return { decision: "allow" };
            },
          },
        }),
      ],
    });
    const first = await lead.run("hi", { store });
    thread = first.thread;
    const second = await lead.run("review", { store, thread: first.thread });
    expect(second.status).toBe("cancelled");
    const { log } = await openStore(store);
    const all = knownEvents(unwrap(log.read(first.thread.branch)));
    const rest = afterBarrier(all);
    expect(rest).not.toContain("agent_spawned");
    expect(rest).toContain("hook_decision");
    expect(child.remaining()).toBe(1);
    expect(all.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});

const overloaded = { error: { reason: "overloaded", http_status: 529 } };
const limits = (name: string) => ({
  provider: "scripted",
  name,
  context_window: 200_000,
  max_output_tokens: 8192,
  input_billing_bound: "context_window" as const,
});
const POLICY: Policy = {
  models: [limits("scripted-1"), limits("scripted-small")],
  retry: {
    ...RETRY_DEFAULTS,
    base_delay_ms: 1,
    max_delay_ms: 1,
    fallback_after: 1,
  },
  fallback: [
    {
      model: { provider: "scripted", name: "scripted-small" },
      model_params: { max_tokens: 1024 },
      adapter: { name: "scripted", version: "1", settings: {} },
      reasoning_carryover: "keep",
    },
  ],
};

/** before_model_switch cancels, then answers `decision`. */
function cancelsThen(
  writer: () => Writer,
  decision: "allow" | "deny",
): LoopExtension {
  return {
    name: "ops",
    timeoutMs: 1_000,
    hooks: {
      before_model_switch: async () => {
        unwrap(writer().append([cancel]));
        return decision === "allow"
          ? { decision }
          : { decision, reason: "stay" };
      },
    },
  };
}

describe("a cancel during before_model_switch", () => {
  for (const [decision, work] of [
    ["allow", "settings_changed"],
    ["deny", "retry_scheduled"],
  ] as const) {
    test(`${decision}: no ${work}, the decision recorded, and the turn ends cancelled`, async () => {
      const h = harness([], [], [], undefined, POLICY);
      const writer = unwrap(h.store.acquire(ROOT, "owner"));
      const primary = scriptedModel({ responses: [overloaded, say("never")] });
      const small = scriptedSmall([say("never")]);
      await resume(
        writer,
        h.artifacts,
        h.config({
          models: (ref) => (ref.name === "scripted-small" ? small : primary),
          extensions: [cancelsThen(() => writer, decision)],
        }),
        { input: userInput("go") },
      );
      const log = events(writer);
      const rest = afterBarrier(log);
      expect(rest).not.toContain(work);
      expect(rest).toContain("hook_decision");
      expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
    });
  }
});

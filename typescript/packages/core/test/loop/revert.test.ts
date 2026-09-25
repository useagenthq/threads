import { describe, expect, test } from "bun:test";
import type { LoopExtension } from "../../src/hooks/types";
import type { KnownEvent, Policy } from "../../src/log";
import { RETRY_DEFAULTS, resume } from "../../src/loop";
import { scriptedModel } from "../../src/model";
import type { EventDraft } from "../../src/store";
import { scriptedSmall } from "../agent/kit";
import { ROOT, unwrap } from "../store/helpers";
import { events, type Harness, harness, userInput } from "./harness";

// The turn-scoped revert across a crash: killed after the next input is durable and before the
// revert batch, the reopened branch reverts exactly once; killed after a recorded deny for that
// input, it never asks before_model_switch again and stays on the fallback.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
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

type Input = Extract<KnownEvent, { type: "user_input" }>;

/** before_model_switch that allows, counting how often it was asked. */
function allowing() {
  const counted = { asked: 0 };
  const ext: LoopExtension = {
    name: "ops",
    timeoutMs: 50,
    hooks: {
      before_model_switch: async () => {
        counted.asked += 1;
        return { decision: "allow" };
      },
    },
  };
  return { counted, ext };
}

/**
 * Turn 1 falls back to the small model, which answers. Then a run is killed after the next
 * input (and `more`) are durable: its lease lapses, nothing else is written.
 */
async function killedAfterInput(
  more: (u: Input) => readonly EventDraft[],
): Promise<{
  readonly h: Harness;
  readonly u: Input;
  readonly left: readonly KnownEvent[];
}> {
  const h = await harness([], [], [], undefined, POLICY);
  const first = unwrap(await h.store.acquire(ROOT, "first"));
  const primary = scriptedModel({ responses: [overloaded] });
  const small = scriptedSmall([say("from the fallback")]);
  const config = h.config({
    models: (ref) => (ref.name === "scripted-small" ? small : primary),
  });
  await resume(first, h.artifacts, config, { input: userInput("hi") });
  await first.release();
  const killed = unwrap(await h.store.acquire(ROOT, "killed", 1));
  unwrap(await killed.append([userInput("again")]));
  const u = events(killed).findLast((e): e is Input => e.type === "user_input");
  if (u === undefined) throw new Error("the input was appended");
  const extra = more(u);
  if (extra.length > 0) unwrap(await killed.append(extra));
  h.clock.now += 10;
  return { h, u, left: events(killed) };
}

/**
 * Resumes the branch with no new input. `requested` holds, for each after_model_switch, how many
 * model_requests the log held when it ran.
 */
async function reopen(
  h: Harness,
  hook: LoopExtension,
  answer: string,
): Promise<{
  readonly log: readonly KnownEvent[];
  readonly requested: readonly number[];
}> {
  const writer = unwrap(await h.store.acquire(ROOT, "reopened"));
  const primary = scriptedModel({ responses: [say(answer)] });
  const small = scriptedSmall([say(answer)]);
  const requested: number[] = [];
  const watch: LoopExtension = {
    name: "watch",
    timeoutMs: 50,
    hooks: {
      after_model_switch: async () => {
        requested.push(prefixes(events(writer)).length);
      },
    },
  };
  const config = h.config({
    models: (ref) => (ref.name === "scripted-small" ? small : primary),
    extensions: [hook, watch],
  });
  const end = await resume(writer, h.artifacts, config);
  expect(end.kind).toBe("idle");
  return { log: events(writer), requested };
}

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
const after = (log: readonly KnownEvent[], u: Input) =>
  log.filter((e) => e.seq > u.seq);

describe("a revert across a crash", () => {
  test("killed before the revert batch: reopening reverts exactly once", async () => {
    const { h, u, left } = await killedAfterInput(() => []);
    const hook = allowing();
    const { log, requested } = await reopen(h, hook.ext, "from the primary");
    const reverts = log.flatMap((e) =>
      e.type === "settings_changed" && e.data.reason === "revert"
        ? [e.data.cause_event_id]
        : [],
    );
    expect(reverts).toEqual([u.event_id]);
    expect(keyed(log, u)).toEqual(["allow"]);
    expect(hook.counted.asked).toBe(1);
    // The reopened turn declares the primary's line 0, as turn 1 first did.
    expect(prefixes(after(log, u))[0]).toBe(prefixes(log)[0]);
    // The revert is its own step: after_model_switch sees it before the request goes out.
    expect(requested).toEqual([prefixes(left).length]);
  });

  test("killed after a recorded deny: reopening never asks again and stays on the fallback", async () => {
    const { h, u, left } = await killedAfterInput((input) => [
      {
        type: "hook_decision",
        type_version: 1,
        critical: true,
        actor: { kind: "host" },
        data: {
          extension: "ops",
          hook: "before_model_switch",
          decision: "deny",
          reason: "stay on this model",
          input_event_id: input.event_id,
        },
      },
    ]);
    const hook = allowing();
    const { log, requested } = await reopen(h, hook.ext, "still the fallback");
    expect(requested).toEqual([]);
    expect(hook.counted.asked).toBe(0);
    expect(keyed(log, u)).toEqual(["deny"]);
    expect(after(log, u).filter((e) => e.type === "settings_changed")).toEqual(
      [],
    );
    // Sent on the fallback epoch: its line 0 is turn 1's fallback request's.
    expect(prefixes(after(log, u))).toEqual(prefixes(left).slice(-1));
  });
});

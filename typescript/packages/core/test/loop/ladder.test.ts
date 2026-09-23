import { describe, expect, test } from "bun:test";
import { credential } from "../../src/agent/secret";
import type { LoopExtension } from "../../src/hooks/types";
import type { KnownEvent, Policy } from "../../src/log";
import { type LoopConfig, resume } from "../../src/loop";
import { CONTEXT_DEFAULTS } from "../../src/loop/policy";
import type { EventDraft } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { events, type Harness, harness, userInput } from "./harness";

// The context ladder through the loop: thresholds on the estimate, L3
// restore after compacted, L4 preflight, and a compacted run continuing from its recorded
// summary without summarizing again. The scripted model's window is 200000, so W = 180000.

const say = (text: string, input: number) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage: { input_tokens: input, output_tokens: 5 },
});
const SUMMARY = say("The user said first.", 900);

function policy(compact: Partial<typeof CONTEXT_DEFAULTS.compact>): Policy {
  return {
    context: {
      ...CONTEXT_DEFAULTS,
      compact: {
        ...CONTEXT_DEFAULTS.compact,
        keep_tail: { tokens: 1 },
        ...compact,
      },
    },
  };
}

async function turn(
  h: Harness,
  text: string,
  config: Partial<LoopConfig> = {},
): Promise<readonly KnownEvent[]> {
  const writer = unwrap(h.store.acquire(ROOT, `owner-${text}`));
  const before = writer.chain.fold.seq;
  const end = await resume(writer, h.artifacts, h.config(config), {
    input: userInput(text),
  });
  expect(end.kind).toBe("idle");
  writer.release();
  return events(writer).filter((e) => e.seq > before);
}

const types = (log: readonly KnownEvent[]): readonly string[] =>
  log.map((e) =>
    e.type === "model_request" && e.data.purpose === "compaction"
      ? "model_request:compaction"
      : e.type,
  );

describe("L2 threshold compaction and L3 restore", () => {
  test("over compact.trigger the loop compacts before the turn request; the next run reads the recorded summary", async () => {
    const h = harness(
      [],
      [],
      [say("one", 160_000), SUMMARY, say("two", 10), say("three", 10)],
      undefined,
      policy({}),
    );
    await turn(h, "first");
    const second = await turn(h, "second");
    expect(types(second)).toEqual([
      "user_input",
      "model_request:compaction",
      "model_response",
      "compacted",
      "model_request",
      "model_response",
      "turn_completed",
    ]);
    const compacted = second.find((e) => e.type === "compacted");
    expect(compacted?.type === "compacted" && compacted.data.trigger).toBe(
      "threshold",
    );
    // Replay: no second summary; the recorded one renders.
    const third = await turn(h, "third");
    expect(types(third)).toEqual([
      "user_input",
      "model_request",
      "model_response",
      "turn_completed",
    ]);
    expect(h.model.remaining()).toBe(0);
  });

  test("restore re-appends skills from the dropped range, then after_compact feeds its injection", async () => {
    const skill: EventDraft = {
      type: "injected",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        source: "skill",
        trust: "trusted_instruction",
        origin: { id: "deploy" },
        text: "Deploy with make deploy.",
      },
    };
    const h = harness(
      [],
      [],
      [say("one", 160_000), SUMMARY, say("two", 10)],
      undefined,
      policy({}),
    );
    await turn(h, "first");
    const writer = unwrap(h.store.acquire(ROOT, "skill"));
    unwrap(writer.append([skill]));
    writer.release();
    const ops: LoopExtension = {
      name: "ops",
      timeoutMs: 50,
      hooks: { after_compact: async () => ["Remember the deploy."] },
    };
    const second = await turn(h, "second", { extensions: [ops] });
    expect(types(second).slice(3, 7)).toEqual([
      "compacted",
      "injected",
      "hook_decision",
      "injected",
    ]);
    const [restored, , fed] = second.slice(4, 7);
    expect(restored?.type === "injected" && restored.data.source).toBe("skill");
    expect(fed?.type === "injected" && fed.data).toMatchObject({
      source: "hook",
      trust: "untrusted_reference",
      text: "Remember the deploy.",
    });
  });

  test("before_compact deny records compaction_failed{hook} and sends no side request", async () => {
    const h = harness(
      [],
      [],
      [say("one", 160_000), say("two", 10)],
      undefined,
      policy({}),
    );
    await turn(h, "first");
    const guard: LoopExtension = {
      name: "guard",
      timeoutMs: 50,
      hooks: {
        before_compact: async () => ({ decision: "deny", reason: "keep all" }),
      },
    };
    const second = await turn(h, "second", { extensions: [guard] });
    expect(types(second)).toEqual([
      "user_input",
      "hook_decision",
      "compaction_failed",
      "model_request",
      "model_response",
      "turn_completed",
    ]);
    const failed = second.find((e) => e.type === "compaction_failed");
    expect(failed?.type === "compaction_failed" && failed.data).toEqual({
      stage: "hook",
      reason: "hook_denied",
    });
  });
});

describe("L4 preflight", () => {
  test("at the window no request is created: preflight, one reactive compaction, then the request", async () => {
    const h = harness(
      [],
      [],
      [say("one", 185_000), SUMMARY, say("two", 10)],
      undefined,
      policy({ trigger: { tokens: 10_000_000 } }),
    );
    await turn(h, "first");
    const second = await turn(h, "second");
    expect(types(second)).toEqual([
      "user_input",
      "context_preflight_blocked",
      "model_request:compaction",
      "model_response",
      "compacted",
      "model_request",
      "model_response",
      "turn_completed",
    ]);
    const blocked = second[1];
    expect(
      blocked?.type === "context_preflight_blocked" && blocked.data,
    ).toMatchObject({
      window_tokens: 180_000,
      action: "compact",
    });
  });

  test("with the breaker open it fails: context_exhausted and nothing sent", async () => {
    const h = harness(
      [],
      [],
      [say("one", 185_000)],
      undefined,
      policy({ trigger: { tokens: 10_000_000 }, max_failures: 1 }),
    );
    await turn(h, "first");
    const writer = unwrap(h.store.acquire(ROOT, "breaker"));
    unwrap(
      writer.append([
        {
          type: "compaction_failed",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: { stage: "summary", reason: "still_over_threshold" },
        },
      ]),
    );
    writer.release();
    const second = await turn(h, "second");
    expect(types(second)).toEqual([
      "user_input",
      "context_preflight_blocked",
      "turn_completed",
    ]);
    const done = second.at(-1);
    expect(done?.type === "turn_completed" && done.data.reason).toBe(
      "context_exhausted",
    );
  });
});

describe("a gate hook that outlives its deadline", () => {
  test("ignoring its abort signal and answering proceed late: recorded failed at the deadline, nothing sent", async () => {
    const h = harness([], [], [say("never", 10)]);
    let late: (() => void) | undefined;
    const slow: LoopExtension = {
      name: "slow",
      timeoutMs: 20,
      hooks: {
        before_model: () => {
          const { promise, resolve } = Promise.withResolvers<unknown>();
          late = () => resolve({ decision: "proceed" });
          return promise;
        },
      },
    };
    const log = await turn(h, "hi", { extensions: [slow] });
    late?.();
    await Bun.sleep(5);
    expect(types(log)).toEqual([
      "user_input",
      "hook_decision",
      "turn_completed",
    ]);
    const recorded = log[1];
    expect(recorded?.type === "hook_decision" && recorded.data.decision).toBe(
      "failed",
    );
    expect(h.model.remaining()).toBe(1);
  });
});

describe("a compaction summary is recorded redacted (C5)", () => {
  test("a key the summary repeats is in neither the log nor the summary artifact", async () => {
    const key = credential("fake", "apiKey", "sk-l9-summary-2e3f", "U")();
    const h = harness(
      [],
      [],
      [say("one", 160_000), say(`The key was ${key}.`, 900), say("two", 10)],
      undefined,
      policy({}),
    );
    await turn(h, "first");
    const second = await turn(h, "second");
    expect(JSON.stringify(second)).not.toContain(key);
    const compacted = second.find((e) => e.type === "compacted");
    if (compacted?.type !== "compacted") throw new Error("the run compacted");
    const summary = h.artifacts.get(compacted.data.summary_ref.sha256);
    if (!summary.ok) throw new Error(summary.error.message);
    expect(new TextDecoder().decode(summary.value)).toBe(
      "The key was [secret fake.apiKey].",
    );
  });
});

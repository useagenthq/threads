import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, ConfigError, scriptedModel, sqlite } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { unwrap } from "../store/helpers";
import { logOf } from "./kit";

// Structured output (ADR 0012 5a): outputRetries is checked at setup, a plain-text end is asked
// again, max_retries failed candidates end the run output_invalid, and a structured subagent
// reports the canonical JSON of its output.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const use = (name: string, input: Record<string, unknown>, id = "c1") => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});
const final = (input: Record<string, unknown>, id = "c1") =>
  use("final_output", input, id);

const Verdict = z.object({ fixed: z.boolean(), tests: z.number().int() });
const GOOD = { fixed: true, tests: 12 };

function only<T extends KnownEvent["type"]>(
  log: readonly KnownEvent[],
  type: T,
): Extract<KnownEvent, { type: T }>[] {
  return log.filter(
    (e): e is Extract<KnownEvent, { type: T }> => e.type === type,
  );
}

describe("outputRetries", () => {
  test.each([-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY])(
    "%p is refused as invalid_config naming the option",
    async (outputRetries) => {
      const bot = agent({
        model: scriptedModel({ responses: [] }),
        output: Verdict,
        outputRetries,
      });
      const checked = await bot.check();
      expect(checked).toMatchObject({
        ok: false,
        error: { code: "invalid_config" },
      });
      expect(checked.ok ? "" : checked.error.message).toContain(
        "outputRetries",
      );
      await expect(
        bot.run("hi", { store: sqlite(":memory:") }),
      ).rejects.toBeInstanceOf(ConfigError);
    },
  );

  test("zero retries end the run at the first failed candidate", async () => {
    const bot = agent({
      model: scriptedModel({ responses: [final({ fixed: "yes" })] }),
      output: Verdict,
      outputRetries: 0,
    });
    const result = await bot.run("Fixed?", { store: sqlite(":memory:") });
    expect(result).toMatchObject({
      status: "failed",
      error: { code: "output_invalid" },
    });
  });
});

describe("failed candidates", () => {
  test('a string "1" for an integer is rejected, never coerced', async () => {
    const bot = agent({
      model: scriptedModel({
        responses: [final({ fixed: true, tests: "1" }), final(GOOD, "c2")],
      }),
      output: Verdict,
    });
    const result = await bot.run("Fixed?", { store: sqlite(":memory:") });
    if (result.status !== "completed") throw new Error(result.status);
    expect(result.output).toEqual(GOOD);
    const previews = only(await logOf(result.thread), "tool_result").map(
      (r) => r.data.preview,
    );
    expect(previews[0]).toStartWith("final_output rejected: ");
    expect(previews[1]).toBe('{"fixed":true,"tests":12}');
  });

  test("a plain-text end is asked for final_output", async () => {
    const bot = agent({
      model: scriptedModel({ responses: [say("It is fixed."), final(GOOD)] }),
      output: Verdict,
    });
    const result = await bot.run("Fixed?", { store: sqlite(":memory:") });
    expect(result).toMatchObject({ status: "completed", output: GOOD });
    const asks = only(await logOf(result.thread), "injected");
    expect(asks.map((a) => [a.data.origin.id, a.data.trust])).toEqual([
      ["final_output", "trusted_instruction"],
    ]);
  });

  test("a rejection and a plain-text end are max_retries failures: output_invalid", async () => {
    const bot = agent({
      model: scriptedModel({
        responses: [final({ fixed: "yes" }), say("It is fixed.")],
      }),
      output: Verdict,
    });
    const result = await bot.run("Fixed?", { store: sqlite(":memory:") });
    expect(result).toMatchObject({
      status: "failed",
      error: { code: "output_invalid" },
    });
  });
});

describe("a structured subagent", () => {
  test("reports the canonical JSON of its accepted output", async () => {
    const checker = agent({
      name: "checker",
      model: scriptedModel({ responses: [final(GOOD)] }),
      output: Verdict,
    });
    const lead = agent({
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "checker", prompt: "Fixed?" }),
          say("It is fixed."),
        ],
      }),
      subagents: [checker],
    });
    const store = sqlite(":memory:");
    const result = await lead.run("Ask the checker.", { store });
    expect(result.status).toBe("completed");
    const events = await logOf(result.thread);
    const shown = '{"fixed":true,"tests":12}';
    expect(only(events, "tool_result")[0]?.data.preview).toBe(shown);
    const ref = only(events, "agent_finished")[0]?.data.output_ref;
    if (ref === undefined) throw new Error("no output_ref");
    const { artifacts } = await openStore(store);
    expect(new TextDecoder().decode(unwrap(artifacts.get(ref.sha256)))).toBe(
      shown,
    );
  });
});

type PartShape = {
  readonly code: string;
  readonly size: "s" | "m";
  readonly parts?: readonly PartShape[] | undefined;
};
const Part: z.ZodType<PartShape> = z.object({
  code: z
    .string()
    .min(2)
    .max(4)
    .regex(/^[A-Z]+$/),
  size: z.enum(["s", "m"]),
  get parts() {
    return z.array(Part).max(2).optional();
  },
});
const Estimate = z.object({
  hours: z.number().int().min(1).max(40),
  root: Part,
});

describe("constraints, enums and recursive schemas", () => {
  test("are checked at every depth and never stop the run", async () => {
    const part = { code: "AB", size: "s", parts: [{ code: "CD", size: "m" }] };
    const deepBad = { ...part, parts: [{ code: "e", size: "m" }] };
    const bot = agent({
      model: scriptedModel({
        responses: [
          final({ hours: 41, root: part }),
          final({ hours: 8, root: deepBad }, "c2"),
          final({ hours: 8, root: part }, "c3"),
        ],
      }),
      output: Estimate,
      outputRetries: 3,
    });
    const result = await bot.run("Estimate it.", { store: sqlite(":memory:") });
    expect(result).toMatchObject({
      status: "completed",
      output: { hours: 8, root: part },
    });
    const outcomes = only(await logOf(result.thread), "output_validated").map(
      (v) => v.data.outcome,
    );
    expect(outcomes).toEqual(["rejected", "rejected", "accepted"]);
  });
});

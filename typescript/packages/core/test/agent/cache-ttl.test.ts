import { describe, expect, test } from "bun:test";
import { agent, type Model, type ModelInfo, sqlite } from "../../src";
import { scriptedModel } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import { logOf } from "./kit";

// The default context.cache_ttl_ms from the models' declared cache lifetimes (lane 10): agreeing
// TTLs are pinned, "none" is ignored, and an unknown or disagreeing lifetime needs
// context.cache_ttl_ms, checked at check() and the first run while agent() stays pure.

const said = {
  content: [{ type: "text", text: "Hi." }],
  stop_reason: "end_turn",
  usage: { input_tokens: 10, output_tokens: 2 },
};

/** A scripted model that declares `cache` like the named adapter model would. */
function declaring(name: string, cache: ModelInfo["cache"]): Model {
  const inner = scriptedModel({ responses: [said] });
  const [provider = "", id = ""] = name.split("/");
  const ref = { provider, name: id };
  const { cache: _, ...rest } = inner.info;
  const model = {
    ...inner,
    info: {
      ...rest,
      model: ref,
      limits: { ...inner.info.limits, ...ref },
      ...(cache === undefined ? {} : { cache }),
    },
  };
  markTestKit(model);
  return model;
}

const HOUR = { ttl_ms: 3_600_000 };
const FIVE = { ttl_ms: 300_000 };
const DAY = { ttl_ms: 86_400_000 };

async function pinned(
  primary: Model,
  fallback: readonly Model[],
  context: { readonly cache_ttl_ms?: number } = {},
): Promise<number> {
  const bot = agent({ model: primary, fallback: [...fallback], context });
  const result = await bot.run("hi", { store: sqlite(":memory:") });
  const started = (await logOf(result.thread)).find(
    (e) => e.type === "thread_started",
  );
  if (started?.type !== "thread_started") throw new Error("no thread_started");
  const ttl = started.data.policy?.context?.cache_ttl_ms;
  if (ttl === undefined) throw new Error("no context policy");
  return ttl;
}

async function refused(
  primary: Model,
  fallback: readonly Model[],
): Promise<string> {
  // agent() is pure: the check runs at check() (and the first run).
  const bot = agent({ model: primary, fallback: [...fallback] });
  const checked = await bot.check();
  if (checked.ok) throw new Error("check passed");
  expect(checked.error.code).toBe("invalid_config");
  return checked.error.message;
}

describe("the default cache TTL comes from the models' declared lifetimes", () => {
  test("1h primary and 1h fallback pin 3600000", async () => {
    const a = declaring("anthropic/claude-sonnet-5", HOUR);
    const b = declaring("anthropic/claude-haiku-4-5", HOUR);
    expect(await pinned(a, [b])).toBe(3_600_000);
  });

  test("1h primary and 5m fallback need context.cache_ttl_ms, naming both", async () => {
    const a = declaring("anthropic/claude-sonnet-5", HOUR);
    const b = declaring("anthropic/claude-haiku-4-5", FIVE);
    expect(await refused(a, [b])).toBe(
      "anthropic/claude-sonnet-5 caches for 1h but fallback anthropic/claude-haiku-4-5 for 5m: set context.cache_ttl_ms",
    );
    expect(await pinned(a, [b], { cache_ttl_ms: 3_600_000 })).toBe(3_600_000);
  });

  test("1h primary and an OpenAI fallback (5m, automatic) are refused", async () => {
    const a = declaring("anthropic/claude-sonnet-5", HOUR);
    const b = declaring("openai/gpt-5.5", FIVE);
    expect(await refused(a, [b])).toContain("fallback openai/gpt-5.5 for 5m");
  });

  test("OpenAI models with 24h retention pin 86400000", async () => {
    const a = declaring("openai/gpt-5.5", DAY);
    const b = declaring("openai/gpt-5.5-mini", DAY);
    expect(await pinned(a, [b])).toBe(86_400_000);
  });

  test("5m primary and a 5m OpenAI fallback keep the default", async () => {
    const a = declaring("anthropic/claude-sonnet-5", FIVE);
    const b = declaring("openai/gpt-5.5", FIVE);
    expect(await pinned(a, [b])).toBe(300_000);
  });

  test("a fallback that never caches is ignored", async () => {
    const a = declaring("anthropic/claude-sonnet-5", HOUR);
    const b = declaring("anthropic/claude-haiku-4-5", "none");
    expect(await pinned(a, [b])).toBe(3_600_000);
  });

  test("an unknown lifetime is refused, even beside a 5m primary", async () => {
    const hour = declaring("anthropic/claude-sonnet-5", HOUR);
    const five = declaring("anthropic/claude-haiku-4-5", FIVE);
    const bridge = declaring("mistral/large", undefined);
    const why =
      "the cache lifetime of mistral/large is unknown: set context.cache_ttl_ms, or pass cacheTtlMs to its factory";
    expect(await refused(hour, [bridge])).toBe(why);
    expect(await refused(five, [bridge])).toBe(why);
    const declared = declaring("mistral/large", FIVE);
    expect(await pinned(five, [declared])).toBe(300_000);
  });

  test("a model alone with an unknown lifetime needs it declared", async () => {
    expect(await refused(declaring("mistral/large", undefined), [])).toContain(
      "is unknown",
    );
    const given = { cache_ttl_ms: 600_000 };
    expect(await pinned(declaring("mistral/large", undefined), [], given)).toBe(
      600_000,
    );
    expect(await pinned(declaring("mistral/large", "none"), [])).toBe(300_000);
  });

  test("agent() stays pure: a disagreement throws nothing until check()", () => {
    const a = declaring("anthropic/claude-sonnet-5", HOUR);
    const b = declaring("anthropic/claude-haiku-4-5", FIVE);
    expect(() => agent({ model: a, fallback: [b] })).not.toThrow();
  });

  test("the scripted model never caches", () => {
    expect(scriptedModel({ responses: [] }).info.cache).toBe("none");
  });
});

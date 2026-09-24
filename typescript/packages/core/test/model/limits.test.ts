import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { type ModelCatalog, parseCatalog } from "../../src/model/catalog";
import { MODEL_CATALOGS } from "../../src/model/generated/catalogs";
import { limitsFrom, modelLimits } from "../../src/model/limits";

// Lane 06: a factory's limits come from the provider's exact-id catalog, per-field overrides
// win, and the default output cap is min(8192, max_output_tokens).

const SPEC = join(import.meta.dir, "../../../../../spec");
const Vectors = z.object({
  cases: z.array(
    z.object({ name: z.string(), valid: z.boolean(), catalog: z.unknown() }),
  ),
});
const vectors = Vectors.parse(
  JSON.parse(
    readFileSync(join(SPEC, "conformance/vectors/model-catalog.json"), "utf8"),
  ),
).cases;

const catalog: ModelCatalog = {
  version: 1,
  provider: "acme",
  entries: [
    {
      id: "m-1",
      max_input_tokens: 1000,
      max_output_tokens: 100_000,
      source: "https://acme.test/m-1",
      verified: "2026-09-24",
    },
    {
      id: "m-small",
      max_input_tokens: 1000,
      max_output_tokens: 4096,
      source: "https://acme.test/m-small",
      verified: "2026-09-24",
    },
  ],
  withdrawn: [
    {
      id: "m-small",
      reason: "the page was misread",
      use: { max_input_tokens: 900, max_output_tokens: 2048 },
    },
  ],
};
const limits = (id: string, options = {}) =>
  limitsFrom(catalog, "acme", id, options);

describe("model catalog files (spec/conformance/vectors/model-catalog.json)", () => {
  test.each(vectors.map((v) => [v.name, v] as const))("%s", (_, v) => {
    expect(parseCatalog(JSON.stringify(v.catalog)).ok).toBe(v.valid);
  });

  test("every embedded catalog parses, one per provider", () => {
    const providers = MODEL_CATALOGS.map((text) => {
      const parsed = parseCatalog(text);
      if (!parsed.ok) throw new Error(parsed.error);
      return parsed.value.provider;
    });
    expect(providers).toEqual(["anthropic", "openai"]);
  });
});

describe("modelLimits", () => {
  test("every listed id resolves with no options; the cap is min(8192, max_output_tokens)", () => {
    for (const text of MODEL_CATALOGS) {
      const parsed = parseCatalog(text);
      if (!parsed.ok) throw new Error(parsed.error);
      for (const entry of parsed.value.entries)
        expect(modelLimits(parsed.value.provider, entry.id, {})).toEqual({
          max_input_tokens: entry.max_input_tokens,
          max_output_tokens: entry.max_output_tokens,
          max_tokens: Math.min(8192, entry.max_output_tokens),
        });
    }
    expect(limits("m-1")).toEqual({
      max_input_tokens: 1000,
      max_output_tokens: 100_000,
      max_tokens: 8192,
    });
  });

  test("a small output cap is the default request cap", () => {
    expect(
      limitsFrom(catalog, "acme", "x", {
        maxInputTokens: 10,
        maxOutputTokens: 4096,
      }).max_tokens,
    ).toBe(4096);
  });

  test("each override changes only its own field", () => {
    expect(limits("m-1", { maxInputTokens: 500 })).toEqual({
      max_input_tokens: 500,
      max_output_tokens: 100_000,
      max_tokens: 8192,
    });
    expect(limits("m-1", { maxOutputTokens: 2000 })).toEqual({
      max_input_tokens: 1000,
      max_output_tokens: 2000,
      max_tokens: 2000,
    });
    expect(limits("m-1", { maxTokens: 32_000 }).max_tokens).toBe(32_000);
  });

  test("an unknown id names the options to pass, and only the missing ones", () => {
    expect(() => limits("m-9")).toThrow(
      'acme: unknown model "m-9"; pass maxInputTokens and maxOutputTokens',
    );
    expect(() => limits("m-9", { maxInputTokens: 10 })).toThrow(
      'acme: unknown model "m-9"; pass maxOutputTokens',
    );
    expect(() => limitsFrom(undefined, "aiSdk", "x", {})).toThrow(
      expect.objectContaining({ code: "invalid_config" }),
    );
    expect(limits("m-9", { maxInputTokens: 10, maxOutputTokens: 20 })).toEqual({
      max_input_tokens: 10,
      max_output_tokens: 20,
      max_tokens: 20,
    });
  });

  test("a withdrawn id is refused with its reason, the corrected values and the original ones", () => {
    let message = "";
    try {
      limits("m-small");
    } catch (error) {
      expect(error).toMatchObject({ code: "invalid_config" });
      message = String(error);
    }
    expect(message).toContain("the page was misread");
    expect(message).toContain(
      "maxInputTokens: 900 and maxOutputTokens: 2048 are correct",
    );
    expect(message).toContain(
      "maxInputTokens: 1000 and maxOutputTokens: 4096 continue threads started with the old values",
    );
    expect(
      limits("m-small", { maxInputTokens: 1000, maxOutputTokens: 4096 }),
    ).toEqual({
      max_input_tokens: 1000,
      max_output_tokens: 4096,
      max_tokens: 4096,
    });
  });

  test.each([
    [
      { maxTokens: 0 },
      "maxTokens must be a positive whole number of tokens, not 0",
    ],
    [
      { maxTokens: 1.5 },
      "maxTokens must be a positive whole number of tokens, not 1.5",
    ],
    [
      { maxTokens: 100_001 },
      "maxTokens 100001 is above maxOutputTokens 100000; lower it",
    ],
    [
      { maxInputTokens: -1 },
      "maxInputTokens must be a positive whole number of tokens, not -1",
    ],
    [
      { maxOutputTokens: 0.5 },
      "maxOutputTokens must be a positive whole number of tokens, not 0.5",
    ],
  ])("%p is invalid_config", (options, message) => {
    expect(() => limits("m-1", options)).toThrow(`acme: ${message}`);
    expect(() => limits("m-1", options)).toThrow(
      expect.objectContaining({ code: "invalid_config" }),
    );
  });
});

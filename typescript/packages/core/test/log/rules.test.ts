import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { holds, type Rule } from "../../src/log/rules";

const Named = z.strictObject({ name: z.string() }).meta({ id: "RulesNamed" });

describe("holds", () => {
  test.each<[string, Rule, unknown, boolean]>([
    ["const keeps JSON types apart", { const: 1 }, true, false],
    ["const", { const: "a" }, "a", true],
    ["enum", { enum: ["a", "b"] }, "c", false],
    ["required", { required: ["a"] }, { b: 1 }, false],
    ["required ignores non-objects", { required: ["a"] }, "x", true],
    [
      "properties skip absent keys",
      { properties: { a: { const: 1 } } },
      {},
      true,
    ],
    ["properties", { properties: { a: { const: 1 } } }, { a: 2 }, false],
    ["minProperties", { minProperties: 1 }, {}, false],
    ["minimum", { minimum: 1 }, 0, false],
    ["minimum ignores non-numbers", { minimum: 1 }, "0", true],
    ["not", { not: { required: ["a"] } }, { a: 1 }, false],
    ["anyOf", { anyOf: [{ const: 1 }, { const: 2 }] }, 2, true],
    [
      "oneOf rejects both",
      { oneOf: [{ required: ["a"] }, { required: ["b"] }] },
      { a: 1, b: 1 },
      false,
    ],
    [
      "oneOf rejects neither",
      { oneOf: [{ required: ["a"] }, { required: ["b"] }] },
      {},
      false,
    ],
    [
      "oneOf",
      { oneOf: [{ required: ["a"] }, { required: ["b"] }] },
      { b: 1 },
      true,
    ],
    [
      "allOf",
      { allOf: [{ required: ["a"] }, { required: ["b"] }] },
      { a: 1 },
      false,
    ],
    [
      "then applies when if holds",
      { if: { const: 1 }, then: { const: 2 } },
      1,
      false,
    ],
    [
      "else applies when if fails",
      { if: { const: 1 }, else: { const: 2 } },
      3,
      false,
    ],
    ["no else", { if: { const: 1 }, then: { const: 2 } }, 3, true],
    ["$ref parses with the schema", { $ref: Named }, { name: 1 }, false],
    ["$ref", { $ref: Named }, { name: "a" }, true],
  ])("%s", (_name, rule, value, want) => {
    expect(holds(rule, value)).toBe(want);
  });
});

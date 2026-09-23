import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { fakeSandbox } from "../../src";
import { sha256Hex } from "../../src/hash";
import { builtins } from "../../src/tools";
import {
  AGENT_TOOLS,
  CATALOG,
  entry,
  GATED_TOOLS,
  PROVIDER_TOOLS,
} from "../../src/tools/catalog";

// The shared built-in tool vector (spec/conformance/vectors/tool-inputs.json): every catalog
// input accepts and rejects exactly as authored, and what the agent pins is the catalog.

const SPEC = join(import.meta.dir, "../../../../../spec");
const Vector = z.strictObject({
  description: z.string(),
  catalog_sha256: z.string(),
  cases: z.array(
    z.strictObject({
      tool: z.string(),
      input: z.unknown(),
      valid: z.boolean(),
    }),
  ),
});
const vector = Vector.parse(
  JSON.parse(
    readFileSync(join(SPEC, "conformance/vectors/tool-inputs.json"), "utf8"),
  ),
);
const catalog = readFileSync(join(SPEC, "schema/tools.v1.catalog.json"));

describe("built-in tool catalog", () => {
  for (const c of vector.cases)
    test(`${c.tool} ${JSON.stringify(c.input)} is ${c.valid ? "accepted" : "rejected"}`, () => {
      expect(entry(c.tool).input.safeParse(c.input).success).toBe(c.valid);
    });

  test("the committed catalog is the golden both runtimes pin", () => {
    expect(sha256Hex(catalog)).toBe(vector.catalog_sha256);
  });

  test("every built-in an agent pins is its catalog entry", () => {
    const listed = z
      .array(
        z.strictObject({
          name: z.string(),
          description: z.string(),
          input_schema: z.json(),
        }),
      )
      .parse(JSON.parse(catalog.toString()));
    const pinned = builtins(fakeSandbox(), undefined).map((b) => ({
      name: b.spec.name,
      description: b.spec.description,
      input_schema: b.spec.input_schema,
    }));
    expect<unknown>(pinned).toEqual(
      listed.filter(
        (e) =>
          !AGENT_TOOLS.has(e.name) &&
          !PROVIDER_TOOLS.has(e.name) &&
          !GATED_TOOLS.has(e.name),
      ),
    );
    expect(CATALOG.map((e) => e.name)).toEqual(listed.map((e) => e.name));
  });
});

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import {
  block,
  KEPT_TOOLS,
  resolveDefinition,
  type Template,
} from "../../src/team/dynamic";
import { StartInput } from "../../src/tools/team-inputs";

// spec/conformance/vectors/dynamic.json: the framework set F, the exact block bytes, and what
// resolveDefinition returns for each start's fields against a template (or a static agent).

const FILE = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors/dynamic.json",
);
const Doc = z.object({
  kept: z.array(z.string()),
  blocks: z.array(
    z.object({ starter: z.string(), text: z.string(), block: z.string() }),
  ),
  resolve: z.array(
    z.object({
      name: z.string(),
      template: z
        .object({ tools: z.array(z.string()), models: z.array(z.string()) })
        .nullable(),
      input: z.record(z.string(), z.json()),
      expect: z.record(z.string(), z.json()),
    }),
  ),
});
const DOC = Doc.parse(JSON.parse(readFileSync(FILE, "utf8")));

describe("dynamic agents vector", () => {
  test("F is the kept set", () => {
    expect([...KEPT_TOOLS].toSorted()).toEqual(DOC.kept);
  });

  for (const b of DOC.blocks)
    test(`block from ${b.starter}`, () => {
      expect(block(b.starter, b.text)).toBe(b.block);
    });

  for (const v of DOC.resolve)
    test(v.name, () => {
      const template: Template | undefined = v.template ?? undefined;
      const got = resolveDefinition(template, StartInput.parse(v.input));
      const actual: unknown = got.ok ? { ok: got.value } : { error: got.error };
      expect(actual).toEqual(v.expect);
    });
});

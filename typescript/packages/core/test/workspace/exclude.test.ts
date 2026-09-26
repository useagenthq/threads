import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { excluded } from "../../src/workspace/exclude";

// spec/conformance/vectors/workspace-exclude.json: the deny-list and .git by segments alone,
// so a local_dir pins the same tree in both languages.

const Vector = z.object({
  cases: z.array(z.object({ path: z.string(), excluded: z.boolean() })),
});
const cases = Vector.parse(
  JSON.parse(
    readFileSync(
      join(
        import.meta.dir,
        "../../../../../spec/conformance/vectors/workspace-exclude.json",
      ),
      "utf8",
    ),
  ),
).cases;

describe("workspace-exclude.json", () => {
  for (const c of cases)
    test(`${c.path} → ${c.excluded}`, () => {
      expect(excluded(c.path)).toBe(c.excluded);
    });
});

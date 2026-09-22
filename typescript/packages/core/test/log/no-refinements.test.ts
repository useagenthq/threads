import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

// z.toJSONSchema drops refinements and checks silently, even with unrepresentable: "throw", so
// one would parse differently from the exported schema. Every rule goes through withRule
// (src/log/rules.ts), which exports what it enforces.

const LOG = join(import.meta.dir, "../../src/log");
const ALLOWED = new Set(["rules.ts"]);
const FORBIDDEN = /\.(refine|superRefine|transform|check|overwrite)\s*\(/g;

/** `file:line: call` for each forbidden call in `source`. */
function forbiddenCalls(file: string, source: string): string[] {
  return source
    .split("\n")
    .flatMap((line, i) =>
      [...line.matchAll(FORBIDDEN)].map((m) => `${file}:${i + 1}: ${m[0]}`),
    );
}

function sources(dir: string): string[] {
  return readdirSync(dir, { recursive: true, encoding: "utf8" })
    .filter((path) => path.endsWith(".ts"))
    .map((path) => join(dir, path));
}

describe("the log schema has no refinement the export would drop", () => {
  test("only rules.ts refines", () => {
    const found = sources(LOG)
      .filter((path) => !ALLOWED.has(relative(LOG, path)))
      .flatMap((path) =>
        forbiddenCalls(relative(LOG, path), readFileSync(path, "utf8")),
      );
    expect(found).toEqual([]);
  });

  test("a raw refine is caught", () => {
    const source = 'export const X = z.string().refine((s) => s !== "");';
    expect(forbiddenCalls("x.ts", source)).toEqual(["x.ts:1: .refine("]);
    expect(forbiddenCalls("y.ts", "z.int().superRefine(f)")).toHaveLength(1);
  });
});

import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { main } from "./evals";

// Each example prints what its `// Output:` line says, in process, so the test preload's model
// guard covers it.

test("examples/evals.ts prints its Output line", async () => {
  const source = readFileSync(join(import.meta.dir, "evals.ts"), "utf8");
  const output = /^\/\/ Output: (.*)$/m.exec(source)?.[1];
  expect(await main()).toBe(output ?? "");
});

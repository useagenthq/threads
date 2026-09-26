import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { main as coder } from "./coding-agent";
import { main } from "./evals";
import { main as simulate } from "./evals-simulate";

// Each example prints what its `// Output:` line says, in process, so the test preload's model
// guard covers it.

const printed = (file: string): string => {
  const source = readFileSync(join(import.meta.dir, file), "utf8");
  return /^\/\/ Output: (.*)$/m.exec(source)?.[1] ?? "";
};

test("examples/coding-agent.ts prints its Output line", async () => {
  expect(await coder()).toBe(printed("coding-agent.ts"));
});

test("examples/evals.ts prints its Output line", async () => {
  expect(await main()).toBe(printed("evals.ts"));
});

test("examples/evals-simulate.ts prints its Output line", async () => {
  expect(await simulate()).toBe(printed("evals-simulate.ts"));
});

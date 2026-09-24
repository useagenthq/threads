import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { parseLogLine } from "../../src/log";
import { caseSchema } from "../conformance/schema";

// The team wire contract before its build: the shared line vector parses exactly as authored,
// and every staged case is a valid case whose lines all pass the line schema
// (spec/conformance/README.md, "Staged cases").

const SPEC = join(import.meta.dir, "../../../../../spec");
const STAGED = join(SPEC, "conformance/staged");
const Vector = z.strictObject({
  description: z.string(),
  cases: z.array(
    z.strictObject({ name: z.string(), line: z.string(), valid: z.boolean() }),
  ),
});
const vector = Vector.parse(
  JSON.parse(
    readFileSync(join(SPEC, "conformance/vectors/team-wire.json"), "utf8"),
  ),
);

function outcome(line: string): string {
  const parsed = parseLogLine(line);
  return parsed.ok ? parsed.value.kind : parsed.error.code;
}

describe("team wire vector", () => {
  for (const c of vector.cases)
    test(`${c.name} is ${c.valid ? "admitted" : "invalid_line"}`, () => {
      expect(outcome(c.line)).toBe(c.valid ? "event" : "invalid_line");
    });
});

const json = (path: string): unknown =>
  JSON.parse(readFileSync(join(STAGED, path), "utf8"));

function logs(dir: string): string[] {
  const names = readdirSync(join(STAGED, dir));
  if (names.includes("log.jsonl")) return [`${dir}/log.jsonl`];
  return readdirSync(join(STAGED, dir, "logs")).map((f) => `${dir}/logs/${f}`);
}

describe("staged cases", () => {
  for (const dir of readdirSync(STAGED))
    test(`${dir} is a valid case and every line passes the line schema`, () => {
      expect(
        caseSchema("Case").safeParse(json(`${dir}/case.json`)).success,
      ).toBe(true);
      expect(
        caseSchema("Expected").safeParse(json(`${dir}/expected.json`)).success,
      ).toBe(true);
      for (const file of logs(dir)) {
        const lines = readFileSync(join(STAGED, file), "utf8").split("\n");
        for (const line of lines.filter((l) => l !== ""))
          expect(outcome(line)).not.toBe("invalid_line");
      }
    });
});

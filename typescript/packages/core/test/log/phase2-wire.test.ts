import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { parseLogLine } from "../../src/log";
import { verifyExport } from "../../src/verify/verify";
import { caseSchema } from "../conformance/schema";

// Teams Phase 2's supervision before its build (spec/conformance/README.md, "Staged cases"):
// every case still under staged-phase-2 is a valid case whose lines all pass the line schema, and
// a reader refuses its supervision forms as unsupported_critical_event, never reducing one as
// ordinary work. The rest of Phase 2 is live (lane 29D) and its cases are in cases/.

const STAGED = join(
  import.meta.dir,
  "../../../../../spec/conformance/staged-phase-2",
);
const json = (path: string): unknown =>
  JSON.parse(readFileSync(join(STAGED, path), "utf8"));
const logs = (dir: string): string[] =>
  readdirSync(join(STAGED, dir, "logs")).map((f) => `${dir}/logs/${f}`);
const bytes = (file: string): Uint8Array =>
  new Uint8Array(readFileSync(join(STAGED, file)));

/** The code a reader returns for a log: ok, or its error's code and seq. */
function read(file: string): { readonly code: string; readonly seq: number } {
  const verified = verifyExport(bytes(file));
  return verified.ok
    ? { code: "ok", seq: 0 }
    : { code: verified.error.code, seq: verified.error.seq ?? -1 };
}

describe("staged Phase 2 cases", () => {
  for (const dir of readdirSync(STAGED))
    test(`${dir} is a valid case, its lines parse, and its Phase 2 logs are refused`, () => {
      expect(
        caseSchema("Case").safeParse(json(`${dir}/case.json`)).success,
      ).toBe(true);
      expect(
        caseSchema("Expected").safeParse(json(`${dir}/expected.json`)).success,
      ).toBe(true);
      const codes = logs(dir).map((file) => {
        const lines = readFileSync(join(STAGED, file), "utf8").split("\n");
        for (const line of lines.filter((l) => l !== ""))
          expect(parseLogLine(line).ok).toBe(true);
        return read(file).code;
      });
      for (const code of codes)
        expect(["ok", "unsupported_critical_event"]).toContain(code);
      expect(codes).toContain("unsupported_critical_event");
    });
});

describe("supervision forms before lane 29E", () => {
  const dir = "host-supervisor-restart/logs";
  test("the host team log reads up to its supervisor_decided", () => {
    const lines = readFileSync(join(STAGED, `${dir}/team.jsonl`), "utf8");
    const decided = lines
      .split("\n")
      .findIndex((l) => l.includes('"type":"supervisor_decided"'));
    expect(read(`${dir}/team.jsonl`)).toEqual({
      code: "unsupported_critical_event",
      seq: decided,
    });
  });
  test("a host member's own log is live: nothing of it is a supervision form", () => {
    expect(read(`${dir}/billing.jsonl`)).toEqual({ code: "ok", seq: 0 });
  });
});

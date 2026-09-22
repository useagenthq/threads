import { describe, expect, test } from "bun:test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { type ParseError, parseLogLine } from "../../src/log";

// Schema-level conformance: every line of every case log parses, except the torn tail of an
// interrupted export and the line a negative case breaks on purpose (spec/conformance/README.md).

const CASES = join(import.meta.dir, "../../../../../spec/conformance/cases");
const LINE_CODES = new Set<string>([
  "invalid_line",
  "unsupported_format",
  "unsupported_critical_event",
]);

type LineFailure = { readonly index: number; readonly error: ParseError };

function expectedLineError(
  caseDir: string,
): { code: string; seq?: number } | undefined {
  const expected: unknown = JSON.parse(
    readFileSync(join(caseDir, "expected.json"), "utf8"),
  );
  if (
    typeof expected !== "object" ||
    expected === null ||
    !("error" in expected)
  )
    return undefined;
  const error = expected.error;
  if (
    typeof error !== "object" ||
    error === null ||
    !("code" in error) ||
    typeof error.code !== "string"
  ) {
    return undefined;
  }
  if (!LINE_CODES.has(error.code)) return undefined;
  return "seq" in error && typeof error.seq === "number"
    ? { code: error.code, seq: error.seq }
    : { code: error.code };
}

function parseLine(bytes: Uint8Array): ParseError | undefined {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return { code: "invalid_line", message: "invalid UTF-8" };
  }
  const result = parseLogLine(text);
  return result.ok ? undefined : result.error;
}

/** Splits an export on "\n". A last line without its newline is a torn tail. */
function splitLines(bytes: Uint8Array): { lines: Uint8Array[]; torn: boolean } {
  const lines: Uint8Array[] = [];
  let start = 0;
  for (let i = bytes.indexOf(0x0a); i !== -1; i = bytes.indexOf(0x0a, start)) {
    lines.push(bytes.subarray(start, i));
    start = i + 1;
  }
  const torn = start < bytes.length;
  if (torn) lines.push(bytes.subarray(start));
  return { lines, torn };
}

const caseNames = readdirSync(CASES).toSorted();

describe("conformance: every log line parses", () => {
  test("the corpus is present", () => {
    expect(caseNames.length).toBeGreaterThan(0);
  });

  for (const name of caseNames) {
    const caseDir = join(CASES, name);
    const logPath = join(caseDir, "log.jsonl");
    if (!existsSync(logPath)) continue; // intake and policy cases have no log.

    test(name, () => {
      const { lines, torn } = splitLines(new Uint8Array(readFileSync(logPath)));
      const failures: LineFailure[] = lines.flatMap((bytes, index) => {
        const error = parseLine(bytes);
        return error === undefined ? [] : [{ index, error }];
      });
      const tornIndex = torn ? lines.length - 1 : -1;
      if (torn) {
        expect(failures.find((f) => f.index === tornIndex)?.error.code).toBe(
          "invalid_line",
        );
      }
      const unexpected = failures.filter((f) => f.index !== tornIndex);
      const expected = expectedLineError(caseDir);
      if (expected === undefined) {
        expect(unexpected).toEqual([]);
        return;
      }
      expect(unexpected).toHaveLength(1);
      expect<string | undefined>(unexpected[0]?.error.code).toBe(expected.code);
      if (expected.seq !== undefined)
        expect(unexpected[0]?.error.seq).toBe(expected.seq);
    });
  }
});

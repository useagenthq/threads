import { describe, expect, test } from "bun:test";
import { sha256Hex } from "../../src/hash";
import { verifyExport } from "../../src/verify";
import { envelope, header, jcs } from "../log/fixtures";

// Invalid input at the import boundary that the conformance corpus doesn't cover.

const utf8 = new TextEncoder();
const HEADER = jcs(header);

/** Event lines chained from the header, seq 1.., each built from (type, data). */
function events(...specs: readonly [string, unknown][]): string[] {
  const lines: string[] = [];
  let previous = HEADER;
  for (const [index, [type, data]] of specs.entries()) {
    const seq = index + 1;
    const line = jcs(
      envelope(type, data, {
        seq,
        event_id: `0192e000-0000-7000-8000-${seq.toString(16).padStart(12, "0")}`,
        prev_hash: sha256Hex(previous),
        // An unknown type is admissible only when it is not critical.
        critical: type !== "telemetry_ping",
      }),
    );
    lines.push(line);
    previous = line;
  }
  return lines;
}

function head(seq: number, last: string): string {
  return jcs({
    format: "threads.head",
    format_version: 1,
    branch_id: header.branch_id,
    seq,
    hash: sha256Hex(last),
  });
}

function exportOf(...lines: readonly string[]): Uint8Array {
  return utf8.encode(lines.map((line) => `${line}\n`).join(""));
}

function outcome(bytes: Uint8Array): unknown {
  const result = verifyExport(bytes);
  return result.ok ? "ok" : [result.error.code, result.error.seq];
}

const turn = events(
  ["telemetry_ping", {}],
  ["turn_completed", { reason: "end_turn" }],
);

describe("verifyExport rejects", () => {
  test("an empty export", () => {
    expect(outcome(new Uint8Array())).toEqual(["invalid_line", 0]);
  });

  test("a line that is not UTF-8, named by position", () => {
    const bytes = new Uint8Array([...exportOf(HEADER), 0xff, 0xfe, 0x0a]);
    expect(outcome(bytes)).toEqual(["invalid_line", 1]);
  });

  test("an event before any header", () => {
    const [first = ""] = turn;
    expect(outcome(exportOf(first))).toEqual(["invalid_line", 1]);
  });

  test("a line after the head checkpoint", () => {
    const [first = ""] = turn;
    const bytes = exportOf(HEADER, first, head(1, first), first);
    expect(outcome(bytes)).toEqual(["invalid_line", 1]);
  });

  test("a fork that does not open a child segment", () => {
    const [fork = ""] = events([
      "fork",
      {
        parent_branch_id: header.branch_id,
        at_hash: "a".repeat(64),
        reason: "repair",
      },
    ]);
    expect(outcome(exportOf(HEADER, fork))).toEqual(["invalid_transition", 1]);
  });

  test("a head that names another branch", () => {
    const [first = ""] = turn;
    const other = head(1, first).replace(
      header.branch_id,
      "0192b000-0000-7000-8000-00000000000f",
    );
    expect(outcome(exportOf(HEADER, first, other))).toEqual([
      "head_mismatch",
      1,
    ]);
  });
});

describe("verifyExport accepts", () => {
  test("a log without a head, as unverified", () => {
    const result = verifyExport(exportOf(HEADER, ...turn));
    expect(result.ok && result.value.headVerified).toBe(false);
  });

  test("a verified log, counting committed bytes without the head", () => {
    const last = turn.at(-1) ?? "";
    const body = exportOf(HEADER, ...turn);
    const result = verifyExport(exportOf(HEADER, ...turn, head(2, last)));
    if (!result.ok) throw new Error(result.error.message);
    expect(result.value.headVerified).toBe(true);
    expect(result.value.committedBytes).toBe(body.length);
  });
});

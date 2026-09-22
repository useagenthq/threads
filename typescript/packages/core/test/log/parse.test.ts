import { describe, expect, test } from "bun:test";
import { parseLogLine } from "../../src/log";
import { envelope, HASH, header, kind, parse } from "./fixtures";

describe("framing lines", () => {
  test("header and head parse", () => {
    expect(kind(header)).toBe("header");
    expect(
      kind({
        format: "threads.head",
        format_version: 1,
        branch_id: header.branch_id,
        seq: 0,
        hash: HASH,
      }),
    ).toBe("head");
  });

  test("a newer format_version is unsupported, not corrupt", () => {
    expect(kind({ ...header, format_version: 2 })).toBe("unsupported_format");
  });

  test("unknown keys and bad ids are invalid", () => {
    expect(kind({ ...header, extra: 1 })).toBe("invalid_line");
    expect(
      kind({ ...header, thread_id: "0192A000-0000-7000-8000-000000000001" }),
    ).toBe("invalid_line");
  });
});

describe("strict JSON admission", () => {
  const good = JSON.stringify(header);
  test.each([
    [
      "duplicate key",
      good.replace(
        '"format":"threads.log"',
        '"format":"threads.log","format":"threads.log"',
      ),
    ],
    [
      "duplicate key after escapes",
      good.replace(
        '"format":"threads.log"',
        '"format":"threads.log","\\u0066ormat":"x"',
      ),
    ],
    [
      "lone surrogate escape",
      good.replace('"threads-ts"', '"threads-ts\\ud800"'),
    ],
    ["non-finite number", good.replace("1790000000000", "1e400")],
    ["unsafe integer", good.replace("1790000000000", "9007199254740993")],
    [
      "integer spelled with a fraction",
      good.replace("1790000000000", "1790000000000.0"),
    ],
    [
      "integer spelled with an exponent",
      good.replace("1790000000000", "1.79e12"),
    ],
    ["trailing garbage", `${good}x`],
    ["not an object", "[1]"],
    ["torn", good.slice(0, -5)],
  ])("%s is invalid_line", (_name, line) => {
    const result = parseLogLine(line);
    expect(result.ok ? "ok" : result.error.code).toBe("invalid_line");
  });

  test("a __proto__ key stays data", () => {
    expect(parseLogLine('{"__proto__":{"format":"threads.log"}}').ok).toBe(
      false,
    );
  });

  function injected(text: string): string {
    return JSON.stringify(
      envelope("injected", {
        source: "hook",
        trust: "trusted_instruction",
        origin: { id: "h" },
        text,
      }),
    );
  }

  test("a line just under 1 MiB parses", () => {
    expect(parseLogLine(injected("é".repeat(500_000))).ok).toBe(true);
  });

  test("a line over 1 MiB is invalid", () => {
    const result = parseLogLine(injected("é".repeat(530_000)));
    expect(result.ok ? "ok" : result.error.message).toBe("line exceeds 1 MiB");
  });
});

describe("critical vs ignorable", () => {
  test("an unknown non-critical event is kept", () => {
    expect(
      kind(envelope("telemetry_ping", { note: 1 }, { critical: false })),
    ).toBe("unknown_event");
  });

  test("an unknown critical event refuses with its seq", () => {
    const result = parse(envelope("approval_quorum", {}, { seq: 4 }));
    expect(result.ok ? undefined : result.error).toMatchObject({
      code: "unsupported_critical_event",
      seq: 4,
    });
  });

  test("a known type at a newer version is unknown", () => {
    expect(
      kind(envelope("turn_completed", { anything: true }, { type_version: 2 })),
    ).toBe("unsupported_critical_event");
  });

  test("a known type with the wrong pinned critical value is invalid", () => {
    expect(
      kind(
        envelope("turn_completed", { reason: "end_turn" }, { critical: false }),
      ),
    ).toBe("invalid_line");
    expect(
      kind(
        envelope("park_escalated", { address: { kind: "input", id: "c1" } }),
      ),
    ).toBe("invalid_line");
  });
});

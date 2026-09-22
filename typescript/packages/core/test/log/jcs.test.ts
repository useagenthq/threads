import { describe, expect, test } from "bun:test";
import type { z } from "zod";
import { canonicalize } from "../../src/log";

function text(value: Parameters<typeof canonicalize>[0]): string {
  const result = canonicalize(value);
  return result.ok ? result.value : `error: ${result.error.message}`;
}

function fromBits(hex: string): number {
  const view = new DataView(new ArrayBuffer(8));
  view.setBigUint64(0, BigInt(`0x${hex}`));
  return view.getFloat64(0);
}

describe("RFC 8785 Appendix B: number serialization", () => {
  test.each([
    ["0000000000000000", "0"],
    ["8000000000000000", "0"],
    ["0000000000000001", "5e-324"],
    ["8000000000000001", "-5e-324"],
    ["7fefffffffffffff", "1.7976931348623157e+308"],
    ["ffefffffffffffff", "-1.7976931348623157e+308"],
    ["4340000000000000", "9007199254740992"],
    ["c340000000000000", "-9007199254740992"],
    ["4430000000000000", "295147905179352830000"],
    ["44b52d02c7e14af5", "9.999999999999997e+22"],
    ["44b52d02c7e14af6", "1e+23"],
    ["44b52d02c7e14af7", "1.0000000000000001e+23"],
    ["444b1ae4d6e2ef4e", "999999999999999700000"],
    ["444b1ae4d6e2ef4f", "999999999999999900000"],
    ["444b1ae4d6e2ef50", "1e+21"],
    ["3eb0c6f7a0b5ed8c", "9.999999999999997e-7"],
    ["3eb0c6f7a0b5ed8d", "0.000001"],
    ["41b3de4355555553", "333333333.3333332"],
    ["41b3de4355555554", "333333333.33333325"],
    ["41b3de4355555555", "333333333.3333333"],
    ["41b3de4355555556", "333333333.3333334"],
    ["41b3de4355555557", "333333333.33333343"],
    ["becbf647612f3696", "-0.0000033333333333333333"],
    ["43143ff3c1cb0959", "1424953923781206.2"],
  ])("%s → %s", (bits, expected) => {
    expect(text(fromBits(bits))).toBe(expected);
  });

  test.each(["7fffffffffffffff", "7ff0000000000000", "fff0000000000000"])(
    "%s (NaN or Infinity) is rejected",
    (bits) => {
      expect(text(fromBits(bits))).toBe("error: non-finite number");
    },
  );
});

// Characters are built from code points so the source stays ASCII and escape-free.
const ch = (...points: number[]): string => String.fromCodePoint(...points);
const esc = (hex: string): string => `${"\\"}u${hex}`;

describe("RFC 8785 §3.2", () => {
  test("the sample object (§3.2.2)", () => {
    const value = {
      numbers: [
        Number("333333333.33333329"),
        1e30,
        4.5,
        2e-3,
        0.000000000000000000000000001,
      ],
      string: `${ch(0x20ac)}$${ch(0x0f, 0x0a)}A'B"${"\\"}${"\\"}"/`,
      literals: [null, true, false],
    };
    const expected =
      '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],' +
      `"string":"${ch(0x20ac)}$${esc("000f")}${"\\"}nA'B${"\\"}"${"\\\\\\\\"}${"\\"}"/"}`;
    expect(text(value)).toBe(expected);
  });

  test("keys sort by UTF-16 code units (§3.2.3)", () => {
    const sorted: [string, string][] = [
      [ch(0x0d), "Carriage Return"],
      ["1", "One"],
      [ch(0x80), "Control"],
      [ch(0xf6), "Latin Small Letter O With Diaeresis"],
      [ch(0x20ac), "Euro Sign"],
      [ch(0x1f600), "Emoji: Grinning Face"],
      [ch(0xfb33), "Hebrew Letter Dalet With Dagesh"],
    ];
    const input = Object.fromEntries(sorted.toReversed());
    const pairs = sorted.map(
      ([key, value]) => `${JSON.stringify(key)}:${JSON.stringify(value)}`,
    );
    expect(text(input)).toBe(`{${pairs.join(",")}}`);
  });

  test("minimal escapes: other control characters as lowercase hex", () => {
    const input = ch(
      0x00,
      0x1f,
      0x08,
      0x09,
      0x0a,
      0x0c,
      0x0d,
      0x22,
      0x5c,
      0x2f,
      0x2028,
    );
    const b = "\\";
    expect(text(input)).toBe(
      `"${esc("0000")}${esc("001f")}${b}b${b}t${b}n${b}f${b}r${b}"${b}${b}/${ch(0x2028)}"`,
    );
  });

  test("lone surrogates are rejected, in values and keys", () => {
    expect(text(ch(0xd800))).toBe("error: lone surrogate in string");
    expect(text({ [ch(0xdc00)]: 1 })).toBe("error: lone surrogate in string");
  });

  test("-0 is spelled 0 and nesting keeps array order", () => {
    expect(text({ b: [3, -0, { d: 1, c: 2 }], a: null })).toBe(
      '{"a":null,"b":[3,0,{"c":2,"d":1}]}',
    );
  });
});

describe("nesting limit (wire rule 2)", () => {
  test("64 levels canonicalize, 65 are an error", () => {
    let deep: z.core.util.JSONType = [];
    for (let i = 1; i < 64; i++) deep = [deep];
    expect(canonicalize(deep).ok).toBe(true);
    expect(canonicalize([deep]).ok).toBe(false);
  });
});

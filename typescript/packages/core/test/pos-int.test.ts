import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { isPosInt } from "../src/pos-int";

// The shared PosInt vector (spec/conformance/vectors/pos-int.json): the one check every public
// entry point makes on a positive-integer option, and the same table Python runs. NaN and the
// infinities are not JSON, so they are asserted here.

const FILE = join(
  import.meta.dir,
  "../../../../spec/conformance/vectors/pos-int.json",
);
const Doc = z.array(
  z.object({ name: z.string(), value: z.unknown(), pos_int: z.boolean() }),
);
const entries = Doc.parse(JSON.parse(readFileSync(FILE, "utf8")));

describe("vectors/pos-int.json", () => {
  test("the file has every row both suites run", () => {
    expect(entries.length).toBeGreaterThan(0);
  });
  for (const entry of entries)
    test(entry.name, () => {
      expect(isPosInt(entry.value)).toBe(entry.pos_int);
    });
});

test("a non-finite number is not a positive integer", () => {
  for (const value of [
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.NEGATIVE_INFINITY,
  ])
    expect(isPosInt(value)).toBe(false);
});

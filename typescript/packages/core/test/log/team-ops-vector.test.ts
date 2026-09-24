import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { parseLogLine } from "../../src/log";
import { canonicalize } from "../../src/log/jcs";

// The team op vector before its build (spec/conformance/vectors/team-ops.json): the file holds to
// its schema, and every event of every world passes the line schema. The Teams build runs the
// vectors themselves (lanes 21C-21F).

const SPEC = join(import.meta.dir, "../../../../../spec");
const read = (path: string): unknown =>
  JSON.parse(readFileSync(join(SPEC, path), "utf8"));
const schema = z.fromJSONSchema(
  z
    .record(z.string(), z.json())
    .parse(read("conformance/team-ops.schema.json")),
);
const doc = read("conformance/vectors/team-ops.json");
const Events = z.object({
  events: z.record(z.string(), z.record(z.string(), z.json())),
});

describe("team op vector", () => {
  test("the file holds to its schema", () => {
    expect(schema.safeParse(doc).success).toBe(true);
  });

  test("an invalid file is refused", () => {
    const broken = {
      ...z.record(z.string(), z.json()).parse(doc),
      constants: {},
    };
    expect(schema.safeParse(broken).success).toBe(false);
  });

  for (const [id, event] of Object.entries(Events.parse(doc).events))
    test(`world event ${id} is a valid line`, () => {
      const line = canonicalize({ ...event, prev_hash: "0".repeat(64) });
      if (!line.ok) throw new Error(`${id} is not canonical JSON`);
      expect(parseLogLine(line.value).ok).toBe(true);
    });
});

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { occurrences, parseCron } from "../src/cron";

// DST rules against the shared vector spec/conformance/vectors/schedule-dst.json.

const Vector = z.object({
  cases: z.array(
    z.object({
      name: z.string(),
      cron: z.string(),
      timezone: z.string(),
      from: z.string(),
      to: z.string(),
      occurrences: z.array(z.string()),
    }),
  ),
});

const vector = Vector.parse(
  JSON.parse(
    readFileSync(
      join(
        import.meta.dir,
        "../../../../spec/conformance/vectors/schedule-dst.json",
      ),
      "utf8",
    ),
  ),
);

const iso = (ms: number): string =>
  new Date(ms).toISOString().replace(".000Z", "Z");

describe("schedule DST vector", () => {
  for (const c of vector.cases) {
    test(c.name, () => {
      const cron = parseCron(c.cron);
      if (!cron.ok) throw new Error(cron.error);
      const at = occurrences(
        cron.value,
        c.timezone,
        Date.parse(c.from),
        Date.parse(c.to),
      );
      expect(at.map(iso)).toEqual(c.occurrences);
    });
  }
});

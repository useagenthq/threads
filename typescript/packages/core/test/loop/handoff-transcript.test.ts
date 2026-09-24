import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { type KnownEvent, parseLogLine } from "../../src/log";
import { handoffTranscript } from "../../src/loop/agents/transcript";

// A handoff keeps its tool context (spec/schema/README.md, "Handoff scope"): the forwarded
// transcript carries the handing-off turn's tool calls and results, capped at the spill
// threshold with the oldest dropped first. The shared vector pins the text.

const SPEC = join(import.meta.dir, "../../../../../spec");
const Vector = z.strictObject({
  description: z.string(),
  cases: z.array(
    z.strictObject({
      name: z.string(),
      cap: z.number().int().positive(),
      lines: z.array(z.string()),
      transcript: z.string(),
    }),
  ),
});
const vector = Vector.parse(
  JSON.parse(
    readFileSync(
      join(SPEC, "conformance/vectors/handoff-transcripts.json"),
      "utf8",
    ),
  ),
);

function events(lines: readonly string[]): readonly KnownEvent[] {
  return lines.map((line) => {
    const parsed = parseLogLine(line);
    if (!parsed.ok || parsed.value.kind !== "event")
      throw new Error(`not an event line: ${line}`);
    return parsed.value.event;
  });
}

describe("handoff transcript vector", () => {
  for (const c of vector.cases)
    test(c.name, () => {
      expect(handoffTranscript(events(c.lines), c.cap)).toBe(c.transcript);
    });
});

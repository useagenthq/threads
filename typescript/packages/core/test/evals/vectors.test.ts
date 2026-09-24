import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { drift } from "../../src/evals/drift";
import { judgeInput, verdicts } from "../../src/evals/judge";
import { JsonValue, KnownEvent, ThreadStartedData } from "../../src/log";

// spec/conformance/vectors (lane 22): the judge input bytes, the verdict rule and drift, as
// both runtimes must compute them.

const VECTORS = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors",
);
const read = (name: string): unknown =>
  JSON.parse(readFileSync(join(VECTORS, name), "utf8"));

const JudgeVectors = z.object({
  vectors: z.array(
    z.object({
      name: z.string(),
      given: z.object({
        task: z.string(),
        events: z.array(KnownEvent),
        answer: JsonValue,
        rubric: z.array(z.string()),
      }),
      input: z.string(),
    }),
  ),
});

const VerdictVectors = z.object({
  vectors: z.array(
    z.object({
      name: z.string(),
      rubric: z.array(z.string()),
      output: z.unknown(),
      expect: z.enum(["accept", "judge_invalid"]),
    }),
  ),
});

const Pin = z.object({
  started: ThreadStartedData,
  mcp: z.array(z.string()),
  setup_extensions: z.array(z.string()),
  setup_providers: z.array(z.enum(["memory", "knowledge"])),
});
const DriftVectors = z.object({
  vectors: z.array(
    z.object({
      name: z.string(),
      recorded: z.object({
        started: ThreadStartedData,
        line0: z.string().optional(),
      }),
      pins: z.array(Pin),
      expect: z.unknown(),
    }),
  ),
});

describe("vectors/judge-input.json", () => {
  for (const v of JudgeVectors.parse(read("judge-input.json")).vectors)
    test(v.name, () => {
      expect(judgeInput(v.given)).toBe(v.input);
    });
});

describe("vectors/verdicts.json", () => {
  for (const v of VerdictVectors.parse(read("verdicts.json")).vectors)
    test(v.name, () => {
      const got = verdicts(v.output, v.rubric);
      expect(got.ok ? "accept" : got.error).toBe(v.expect);
    });
});

describe("vectors/drift.json", () => {
  for (const v of DriftVectors.parse(read("drift.json")).vectors)
    test(v.name, () => {
      const { line0 } = v.recorded;
      const got = drift(
        {
          started: v.recorded.started,
          line0:
            line0 === undefined ? undefined : new TextEncoder().encode(line0),
        },
        v.pins.map((p) => ({
          started: p.started,
          mcp: p.mcp,
          setupExtensions: p.setup_extensions,
          setupProviders: p.setup_providers,
          leadsTeam: false,
        })),
      );
      expect(JSON.parse(JSON.stringify(got))).toEqual(v.expect);
    });
});

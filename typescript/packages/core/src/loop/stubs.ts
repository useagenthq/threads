import { z } from "zod";
import type { StubGateway } from "./types";

// recorded stubs matched by (tool, args_hash, occurrence) and consumed in
// order. occurrence counts earlier invocations of the same (tool, args_hash) in this run. No
// unconsumed match fails closed: the gateway never falls back to a live call.

const Stubs = z.strictObject({
  stubs: z.array(
    z.strictObject({
      tool: z.string(),
      args_hash: z.string().regex(/^[0-9a-f]{64}$/),
      occurrence: z.int().min(0),
      output: z.string(),
      is_error: z.boolean().optional(),
    }),
  ),
});

export type RecordedStubs = StubGateway & {
  readonly consumed: () => number;
  readonly unmatched: () => number;
  /** Recorded stubs no invocation consumed. */
  readonly left: () => number;
};

/** A stub gateway over a StubScript (spec/conformance case.schema.json $defs/StubScript). */
export function recordedStubs(script: unknown): RecordedStubs {
  const { stubs } = Stubs.parse(script);
  const used = new Set<number>();
  const seen = new Map<string, number>();
  let unmatched = 0;
  return {
    answer: (tool, argsHash) => {
      const key = `${tool}\n${argsHash}`;
      const occurrence = seen.get(key) ?? 0;
      seen.set(key, occurrence + 1);
      const at = stubs.findIndex(
        (s, i) =>
          !used.has(i) &&
          s.tool === tool &&
          s.args_hash === argsHash &&
          s.occurrence === occurrence,
      );
      const stub = stubs[at];
      if (stub === undefined) {
        unmatched += 1;
        return undefined;
      }
      used.add(at);
      return { output: stub.output, isError: stub.is_error ?? false };
    },
    consumed: () => used.size,
    unmatched: () => unmatched,
    left: () => stubs.length - used.size,
  };
}

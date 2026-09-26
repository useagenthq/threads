import { z } from "zod";
import { type RecordedStubs, recordedStubs } from "../loop";

// The stub entries a run answers from (spec lane 32, A.3). stubs.json numbers occurrences per
// (tool, args_hash) across the whole recorded conversation, prefix entries first and marked
// `scope: "prefix"`. Picking a scope renumbers what is left from 0, so each key stays a queue
// the run consumes in log order.

const Entry = z.strictObject({
  tool: z.string(),
  args_hash: z.string(),
  occurrence: z.int().min(0),
  output: z.string(),
  is_error: z.boolean().optional(),
  scope: z.literal("prefix").optional(),
});
const StubScript = z.strictObject({ stubs: z.array(Entry) });

/** turn: the saved turn's entries only (offline, and a continued prefix). */
export type StubScope = "turn" | "conversation";

export type StubQueue = RecordedStubs & {
  /** The tool of the first call no entry answered, for `unmatched_external_op`. */
  readonly firstUnmatched: () => string | undefined;
};

export function stubQueue(script: unknown, scope: StubScope): StubQueue {
  const { stubs } = StubScript.parse(script);
  const picked =
    scope === "conversation"
      ? stubs
      : stubs.filter((s) => s.scope === undefined);
  const seen = new Map<string, number>();
  const queue = picked.map(({ tool, args_hash, output, is_error }) => {
    const key = `${tool}\n${args_hash}`;
    const occurrence = seen.get(key) ?? 0;
    seen.set(key, occurrence + 1);
    return {
      tool,
      args_hash,
      occurrence,
      output,
      ...(is_error === undefined ? {} : { is_error }),
    };
  });
  const inner = recordedStubs({ stubs: queue });
  let missed: string | undefined;
  return {
    ...inner,
    answer: (tool, argsHash) => {
      const got = inner.answer(tool, argsHash);
      if (got === undefined && missed === undefined) missed = tool;
      return got;
    },
    firstUnmatched: () => missed,
  };
}

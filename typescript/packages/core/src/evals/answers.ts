import { z } from "zod";
import type { ToolSpec } from "../log";
import type { ToolImpl, ToolRun } from "../loop";
import type { LookupResult } from "../model";
import type { SandboxScript } from "../sandbox/script";
import type { ArtifactStore } from "../store";
import { argsHash } from "../thread/case-files";
import type { RecallRecord, SandboxResult, SandboxResults } from "./files";

// The tool bodies of a rerun: every pinned tool answers from the case, never from a sandbox.
// A saved case's read-only calls answer from sandbox.json v2 by (tool, args_hash, occurrence);
// the corpus's scripted sandbox (the v1 shape) answers by tool name, with provider dedup,
// lookups and process checks. A read-only call the recording never made fails closed.

export type Counters = {
  readonly dispatches: Readonly<Record<string, number>>;
  readonly new_executions: Readonly<Record<string, number>>;
  readonly lookups: Readonly<Record<string, number>>;
};

export type Answers = {
  readonly tools: ReadonlyMap<string, ToolImpl>;
  readonly counters: () => Counters;
  /** Read-only calls with no recorded result. */
  readonly unrecorded: () => number;
  /** Recorded results and recall no call used. */
  readonly left: () => number;
};

/** The recalling tools: their recorded items come back as the call's injections. */
const RECALLS: Readonly<Record<string, RecallRecord["source"]>> = {
  search_memory: "memory",
  search_knowledge: "knowledge",
};

/** A pinned schema compiled for the rerun; anything it can't compile is taken as recorded. */
function inputOf(spec: ToolSpec): z.ZodType {
  if (spec.input_schema === undefined) return z.json();
  try {
    return z.fromJSONSchema(spec.input_schema);
  } catch {
    return z.json();
  }
}

type Counter = { [tool: string]: number };
type Input = Parameters<ToolImpl["run"]>[0];

function bump(map: Counter, tool: string): void {
  map[tool] = (map[tool] ?? 0) + 1;
}

/** v2: the recorded result of this call, found by its key; its full bytes when spilled. */
function recorded(
  results: readonly SandboxResult[],
  used: Set<SandboxResult>,
  artifacts: Pick<ArtifactStore, "get">,
) {
  const seen = new Map<string, number>();
  return async (tool: string, input: Input): Promise<ToolRun | undefined> => {
    const hash = argsHash(input);
    const key = `${tool}\n${hash}`;
    const occurrence = (seen.get(key) ?? 0) + 1;
    seen.set(key, occurrence);
    const r = results.find(
      (x) =>
        x.tool === tool && x.args_hash === hash && x.occurrence === occurrence,
    );
    if (r === undefined) return undefined;
    used.add(r);
    const full =
      r.ref === undefined ? undefined : await artifacts.get(r.ref.sha256);
    return {
      kind: "done",
      output:
        full?.ok === true ? new TextDecoder().decode(full.value) : r.preview,
      isError: r.is_error,
      ...(r.content === undefined ? {} : { content: r.content }),
    };
  };
}

type Scripted = NonNullable<SandboxScript["tools"]>[string];

/** The corpus's scripted sandbox for one tool: dedup, lookup and process answers. */
function scripted(
  spec: ToolSpec,
  t: Scripted,
  counters: { dispatches: Counter; new_executions: Counter; lookups: Counter },
  now: () => number,
): Omit<ToolImpl, "spec" | "input"> {
  return {
    run: async (_input, ctx) => {
      bump(counters.dispatches, spec.name);
      const deduped = t.executed_keys?.[ctx.effectKey];
      if (deduped !== undefined)
        return { kind: "done", output: deduped, isError: false };
      bump(counters.new_executions, spec.name);
      return { kind: "done", output: t.output, isError: t.is_error ?? false };
    },
    reconcile: {
      finality: "final",
      lookup: async (key): Promise<LookupResult<string>> => {
        bump(counters.lookups, spec.name);
        const answer = t.lookup?.[key];
        if (answer === undefined)
          return { status: "unknown", reason: "no answer" };
        if (answer.result === "not_found")
          return { status: answer.final ? "not_found" : "not_found_nonfinal" };
        return answer.final
          ? { status: "found", value: answer.output ?? t.output }
          : { status: "unknown", reason: "found without finality" };
      },
    },
    terminate: async () =>
      t.process === "terminated" ? "terminated" : "unknown",
    providerNow: now,
  };
}

export type AnswerSource = {
  readonly specs: readonly ToolSpec[];
  /** sandbox.json: v2 results, the corpus's scripted sandbox, or nothing. */
  readonly sandbox: SandboxResults | SandboxScript | undefined;
  readonly recall: readonly RecallRecord[];
  readonly artifacts: Pick<ArtifactStore, "get">;
  readonly now: () => number;
};

export function answers(source: AnswerSource): Answers {
  const { sandbox, recall } = source;
  const results =
    sandbox !== undefined && "results" in sandbox ? sandbox.results : [];
  const tools =
    sandbox !== undefined && "results" in sandbox ? {} : (sandbox?.tools ?? {});
  const counters = { dispatches: {}, new_executions: {}, lookups: {} };
  const used = new Set<SandboxResult>();
  const recalled = new Set<RecallRecord>();
  let unrecorded = 0;
  const lookup = recorded(results, used, source.artifacts);
  const nextRecall = (tool: string) => {
    const found = recall.find(
      (r) => r.source === RECALLS[tool] && !recalled.has(r),
    );
    if (found !== undefined) recalled.add(found);
    return found?.items;
  };
  const impls = new Map<string, ToolImpl>();
  for (const spec of source.specs) {
    const t = tools[spec.name];
    const base = { spec, input: inputOf(spec) };
    if (t !== undefined) {
      impls.set(spec.name, {
        ...base,
        ...scripted(spec, t, counters, source.now),
        ...(t.concurrent === true ? { concurrent: true as const } : {}),
      });
      continue;
    }
    impls.set(spec.name, {
      ...base,
      run: async (input) => {
        const run = await lookup(spec.name, input);
        if (run === undefined) {
          unrecorded += 1;
          return {
            kind: "done",
            output: `unrecorded_call: the saved turn made no ${spec.name} call with these arguments`,
            isError: true,
          };
        }
        const inject = run.kind === "done" ? nextRecall(spec.name) : undefined;
        return run.kind === "done" && inject !== undefined
          ? { ...run, inject }
          : run;
      },
    });
  }
  return {
    tools: impls,
    counters: () => counters,
    unrecorded: () => unrecorded,
    left: () => results.length - used.size + recall.length - recalled.size,
  };
}

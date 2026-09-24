import { z } from "zod";
import type { ToolSpec } from "../../src/log";
import type { ToolImpl } from "../../src/loop";
import type { LookupResult } from "../../src/model";
import { SandboxScript } from "../../src/sandbox";
import type { Counters } from "./cases";

// The conformance ScriptedSandbox (case.schema.json $defs/SandboxScript) as tool bodies: a key
// in executed_keys is provider dedup (its output, no new execution), lookup answers
// reconciliation with its own finality, process answers termination.

export type ScriptedTools = {
  readonly tools: ReadonlyMap<string, ToolImpl>;
  readonly counters: () => Required<Counters>;
  /** The conformance policy's decision for a tool: the one its entry names, else allow. */
  readonly decision: (name: string) => "allow" | "ask" | "deny";
};

export function scriptedTools(
  script: unknown,
  specs: readonly ToolSpec[],
  now: () => number,
): ScriptedTools {
  const tools = SandboxScript.parse(script ?? {}).tools ?? {};
  const counters = { dispatches: {}, new_executions: {}, lookups: {} };
  const bump = (kind: keyof typeof counters, tool: string): void => {
    const map: Record<string, number> = counters[kind];
    map[tool] = (map[tool] ?? 0) + 1;
  };
  const impls = new Map<string, ToolImpl>();
  for (const spec of specs) {
    // The case's tools exist only as pinned JSON Schemas; the test kit compiles them. The
    // framework itself never evaluates a pinned schema.
    const input = z.fromJSONSchema(spec.input_schema);
    const t = tools[spec.name];
    if (t === undefined) {
      impls.set(spec.name, {
        spec,
        input,
        run: async () => {
          throw new Error(`the case scripts no body for ${spec.name}`);
        },
      });
      continue;
    }
    impls.set(spec.name, {
      spec,
      input,
      ...(t.concurrent === true ? { concurrent: true } : {}),
      run: async (_input, ctx) => {
        bump("dispatches", spec.name);
        const deduped = t.executed_keys?.[ctx.effectKey];
        if (deduped !== undefined)
          return { kind: "done", output: deduped, isError: false };
        bump("new_executions", spec.name);
        return { kind: "done", output: t.output, isError: t.is_error ?? false };
      },
      reconcile: {
        finality: "final",
        lookup: async (key): Promise<LookupResult<string>> => {
          bump("lookups", spec.name);
          const answer = t.lookup?.[key];
          if (answer === undefined)
            return { status: "unknown", reason: "no answer" };
          if (answer.result === "not_found")
            return {
              status: answer.final ? "not_found" : "not_found_nonfinal",
            };
          return answer.final
            ? { status: "found", value: answer.output ?? t.output }
            : { status: "unknown", reason: "found without finality" };
        },
      },
      terminate: async () =>
        t.process === "terminated" ? "terminated" : "unknown",
      providerNow: now,
    });
  }
  return {
    tools: impls,
    counters: () => counters,
    decision: (name) => tools[name]?.decision ?? "allow",
  };
}

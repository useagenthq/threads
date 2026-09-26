import { CaseSimulate, type SimulateBlocked } from "../evals/files";
import type { KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import type { ArtifactStore } from "../store";
import type { LogError } from "../verify/error";
import { logError } from "../verify/error";
import { numbered, type StubEntry, stubsOf } from "./case-files";

// The `simulate` option of saveCase (spec lane 32, A): the case.json field, and the prefix
// effects a live simulation re-drives against. The prefix's stubs share the saved turn's key
// space and are marked `scope: "prefix"`, so an offline rerun ignores them.

/** api.json SaveCaseOptions.simulate: the TS literal, camelCase, written as snake_case. */
export type SimulateUser =
  | {
      readonly kind: "model";
      readonly persona: string;
      readonly goal: string;
      readonly maxMessages?: number;
    }
  | { readonly kind: "script"; readonly messages: readonly string[] };

/**
 * The wire form: the one camelCase key renamed, every other field passed through untouched, so a
 * value the type system would have rejected still reaches the schema and fails by name.
 */
function wire(simulate: SimulateUser): unknown {
  const { maxMessages, ...rest } = { maxMessages: undefined, ...simulate };
  return maxMessages === undefined
    ? rest
    : { ...rest, max_messages: maxMessages };
}

/** The wire form, checked against the schema: a bad field is invalid_request, by name. */
export function simulateField(
  simulate: SimulateUser,
): Result<CaseSimulate, LogError> {
  const parsed = CaseSimulate.safeParse(wire(simulate));
  if (parsed.success) return ok(parsed.data);
  const issue = parsed.error.issues[0];
  const at = issue?.path.join(".") ?? "kind";
  return err(
    logError(
      "invalid_request",
      `simulate.${at === "" ? "kind" : at}: ${issue?.message ?? "invalid"}`,
    ),
  );
}

const settled = (log: readonly KnownEvent[]): ReadonlySet<string> =>
  new Set(
    log.flatMap((e) =>
      e.type === "effect_commit" || e.type === "effect_resolved"
        ? [e.data.call_id]
        : [],
    ),
  );

/** The prefix's effectful calls nothing ever settled: they have no result to stub. */
function unsettled(
  prefix: readonly KnownEvent[],
  log: readonly KnownEvent[],
): ReadonlySet<string> {
  const done = settled(log);
  return new Set(
    prefix.flatMap((e) =>
      e.type === "effect_begin" && !done.has(e.data.call_id)
        ? [e.data.call_id]
        : [],
    ),
  );
}

/**
 * stubs.json for a simulated case: the prefix's settled effects, then the saved turn's, numbered
 * as one queue per (tool, args_hash) so a live run consumes them in log order. An unsettled
 * prefix effect blocks the simulation instead, and the case stays runnable offline.
 */
export async function prefixStubs(
  prefix: readonly KnownEvent[],
  turn: readonly StubEntry[],
  log: readonly KnownEvent[],
  artifacts: ArtifactStore,
): Promise<
  Result<
    {
      readonly stubs: readonly StubEntry[];
      readonly blocked: SimulateBlocked | undefined;
    },
    LogError
  >
> {
  const open = unsettled(prefix, log);
  const kept = prefix.filter(
    (e) => e.type !== "tool_call" || !open.has(e.data.call_id),
  );
  const built = await stubsOf(kept, artifacts);
  if (!built.ok) return built;
  const earlier = built.value.map(
    (s): StubEntry => ({ ...s, scope: "prefix" }),
  );
  return ok({
    stubs: numbered([...earlier, ...turn]),
    blocked: open.size === 0 ? undefined : "unsettled_effect",
  });
}

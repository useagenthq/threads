import type { HookContext, HookName, LoopExtension } from "../hooks/types";
import { HookDecisionData } from "../log";
import type { ExtensionScript, HookRecord } from "./files";
import { hookValue } from "./hook-values";
import { HOOK_KINDS } from "./kinds";
import { standInOrder } from "./stand-in-order";

// Stand-in extensions (spec lane 22, A.2): the offline rerun loads no user code, so each pinned
// extension is replaced by one with its name that answers from the turn's records. A recorded
// hook is defined only when the turn has a record for it and answers with the next record;
// every observation hook is defined and answers only at the call its record is keyed to.

const HOOKS: readonly HookName[] = HookDecisionData.shape.hook.options;

const TIMED_OUT = /^timed out after (\d+) ms$/;

/** Reproduces a failed outcome: the same throw, bad value or timeout the recording saw. */
async function fail(reason: string, ctx: HookContext): Promise<unknown> {
  if (reason.startsWith("threw: ")) throw new Error(reason.slice(7));
  if (TIMED_OUT.test(reason)) {
    const { promise, resolve } = Promise.withResolvers<void>();
    ctx.signal.addEventListener("abort", () => resolve());
    await promise;
  }
  return { decision: "not a decision" };
}

/** The observation record this call belongs to: its call_id, else its trigger's occurrence. */
function keyed(
  records: readonly HookRecord[],
  n: number,
  ctx: HookContext,
): HookRecord | undefined {
  return records.find((r) => {
    const at = r.at;
    if (at !== undefined && "call_id" in at) return at.call_id === ctx.callId;
    return (at?.occurrence ?? 1) === n;
  });
}

/** after_tool's "no annotation" is an empty list; the other observation hooks return nothing. */
const observationOk = (hook: HookName): unknown =>
  hook === "after_tool" ? [] : undefined;

export type StandIns = {
  readonly extensions: readonly LoopExtension[];
  /** Calls past the records, and records no call reached: each fails the rerun. */
  readonly unrecorded: () => number;
};

type Count = { overrun: number; readonly used: Set<HookRecord> };
type Hook = (args: unknown, ctx: HookContext) => Promise<unknown>;

function answer(
  record: HookRecord,
  ctx: HookContext,
  count: Count,
): Promise<unknown> {
  count.used.add(record);
  return record.decision === "failed"
    ? fail(record.reason ?? "", ctx)
    : Promise.resolve(hookValue(record));
}

/** One hook of a stand-in: the records it answers with, counted per call. */
function hookOf(
  name: string,
  hook: HookName,
  own: readonly HookRecord[],
  count: Count,
): Hook {
  let calls = 0;
  if (HOOK_KINDS[hook] === "observation")
    return async (_args, ctx) => {
      calls += 1;
      const record = keyed(own, calls, ctx);
      return record === undefined
        ? observationOk(hook)
        : answer(record, ctx, count);
    };
  return async (_args, ctx) => {
    calls += 1;
    const record = own.find((r) => r.occurrence === calls);
    if (record !== undefined) return answer(record, ctx, count);
    count.overrun += 1;
    throw new Error(`unrecorded_hook: ${name} ${hook} call ${calls}`);
  };
}

function standIn(
  name: string,
  records: readonly HookRecord[],
  count: Count,
): LoopExtension {
  const hooks: Partial<Record<HookName, Hook>> = {};
  for (const hook of HOOKS) {
    const own = records.filter((r) => r.hook === hook);
    // A recorded hook with no record in this turn: the recording's extension may not define it.
    if (HOOK_KINDS[hook] === "recorded" && own.length === 0) continue;
    hooks[hook] = hookOf(name, hook, own, count);
  }
  const timeout = records
    .map((r) => TIMED_OUT.exec(r.reason ?? "")?.[1])
    .find((ms) => ms !== undefined);
  return {
    name,
    timeoutMs: timeout === undefined ? 5000 : Number(timeout),
    hooks,
  };
}

export function standIns(script: ExtensionScript | undefined): StandIns {
  const records = script?.hooks ?? [];
  const count: Count = { overrun: 0, used: new Set() };
  const extensions = standInOrder(records).map((name) =>
    standIn(
      name,
      records.filter((r) => r.extension === name),
      count,
    ),
  );
  return {
    extensions,
    unrecorded: () => count.overrun + records.length - count.used.size,
  };
}

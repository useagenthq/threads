import type { Agent } from "../agent/agent";
import { dryPin } from "../agent/dry-pin";
import type { ThreadRef } from "../agent/result";
import { openStore } from "../agent/sqlite";
import type { CaseDir } from "./case-dir";
import type { CaseLog } from "./checks";
import { costBound, handleFor } from "./ledger";
import type { Env } from "./live-env";
import type { Simulation } from "./schema";

// The prefix of a simulated case (spec lane 32, B.1): the real turns before the saved one.
// Continue them when the agent's config_hash is unchanged -- the case log is imported and the
// conversation goes on, so the prefix costs no model call -- and re-drive their inputs when it
// changed, so a changed agent is graded on the whole path.

export type Plan = {
  readonly mode: Simulation["prefix"];
  /** Recorded inputs to re-send before the opener; empty unless the prefix is re-driven. */
  readonly redrive: readonly string[];
  readonly turns: number;
  /** The imported thread to continue; absent when the agent starts a new one. */
  readonly thread: ThreadRef | undefined;
  /** Events at or below this seq are the imported prefix, not this conversation's. */
  readonly since: number;
  readonly baseCostNanos: number;
};

/** The recorded user inputs before the saved turn, in log order. */
export const prefixTexts = (log: CaseLog): readonly (string | undefined)[] =>
  log.events.flatMap((e) => (e.type === "user_input" ? [e.data.text] : []));

const recordedHash = (log: CaseLog): string | undefined => {
  const started = log.events.find((e) => e.type === "thread_started");
  return started?.type === "thread_started"
    ? started.data.config_hash
    : undefined;
};

const NEW_THREAD = { thread: undefined, since: 0, baseCostNanos: 0 } as const;

/** Imports the case log into the eval store, so the conversation continues the real thread. */
async function importCase(
  c: CaseDir,
  env: Env,
): Promise<Pick<Plan, "thread" | "since" | "baseCostNanos">> {
  const store = await openStore(env.store);
  for (const artifact of c.artifacts) await store.artifacts.put(artifact);
  const imported = await store.log.importLog(c.log);
  const header = imported.ok
    ? imported.value.segments.at(-1)?.header
    : undefined;
  if (!imported.ok || header === undefined) return NEW_THREAD;
  const thread = {
    id: header.thread_id,
    branch: header.branch_id,
    store: env.store,
  };
  return {
    thread,
    since: imported.value.fold.seq,
    baseCostNanos: await costBound(await handleFor(env, thread)),
  };
}

export async function planPrefix(
  c: CaseDir,
  log: CaseLog,
  target: Agent<never, unknown>,
  env: Env,
): Promise<Plan> {
  const texts = prefixTexts(log).flatMap((t) => (t === undefined ? [] : [t]));
  if (texts.length === 0)
    return { mode: "none", redrive: [], turns: 0, ...NEW_THREAD };
  const same = dryPin(target).started.config_hash === recordedHash(log);
  const imported = same ? await importCase(c, env) : NEW_THREAD;
  return imported.thread === undefined
    ? { mode: "redriven", redrive: texts, turns: texts.length, ...NEW_THREAD }
    : { mode: "continued", redrive: [], turns: texts.length, ...imported };
}

import { existsSync } from "node:fs";
import type { Agent } from "../agent/agent";
import { dryPin } from "../agent/dry-pin";
import { ConfigError } from "../agent/errors";
import type { DryPin } from "../agent/registry";
import { type Store, sqlite, tenantStore } from "../agent/sqlite";
import type { Cost } from "../log";
import { type CaseDir, caseNames, readCase } from "./case-dir";
import {
  type CaseLog,
  driftCheck,
  driftReason,
  replayCheck,
  rerunCheck,
  rerunReason,
  skipReason,
} from "./checks";
import { type Live, type LiveOutcome, liveCheck } from "./live";
import { addCost, report } from "./report";
import type { Checks, EvalCaseResult, EvalReport } from "./schema";

// runEvals() (spec/api.json): every saved case under `cases`, sorted by name, through the checks
// cheapest first. Offline it makes no model call and touches no store the caller passed; live,
// it runs the current agents and a judge under a budget. Per-case outcomes, and a guard abort,
// are values; only a setup mistake (ConfigError) or a bug throws.

export type RunEvalsOptions = {
  /** The directory of saved cases. Default: cases. */
  readonly cases?: string;
  /** Only these case names. */
  readonly only?: readonly string[];
  /** Your current agents: drift compares each case with its agent's config, for free. */
  readonly agents?: readonly Agent<never, unknown>[];
  /** Grade the current agents with a judge model: real model calls, under live.budget. */
  readonly live?: Live;
  /** Keep live and judge threads here (tenant evals). Default: a private in-memory store. */
  readonly store?: Store;
  /** Fail the run on stale and skipped cases too. */
  readonly strict?: boolean;
};

type Plan = {
  readonly root: string;
  readonly pins: readonly DryPin[] | undefined;
  readonly agents: readonly Agent<never, unknown>[];
  readonly live: Live | undefined;
  readonly store: Store;
  readonly kept: boolean;
};

type Evaluated = {
  readonly result: EvalCaseResult;
  readonly live?: LiveOutcome;
};

function checkOptions(o: RunEvalsOptions): void {
  const root = o.cases ?? "cases";
  if (!existsSync(root))
    throw new ConfigError(
      "invalid_config",
      `cases: no directory ${root}; save one with thread.saveCase() or pass cases`,
    );
  if (o.live === undefined) return;
  const missing = [
    ...(o.live.judge === undefined ? ["live.judge"] : []),
    ...(o.live.budget === undefined ? ["live.budget"] : []),
  ];
  if (missing.length > 0)
    throw new ConfigError(
      "invalid_config",
      `live evals need a judge model and a budget: ${missing.join(", ")}`,
    );
  if ((o.agents ?? []).length === 0)
    throw new ConfigError(
      "invalid_config",
      "live evals need agents: pass agents",
    );
}

const result = (
  name: string,
  status: EvalCaseResult["status"],
  checks: Checks,
  reason?: string,
  simulation?: EvalCaseResult["simulation"],
): EvalCaseResult => ({
  name,
  status,
  ...(reason === undefined ? {} : { reason }),
  ...(simulation === undefined ? {} : { simulation }),
  checks,
});

/** The live check's part of a case: its status, reason, simulation and judge result. */
function judgedStatus(
  name: string,
  checks: Checks,
  live: LiveOutcome,
): EvalCaseResult {
  if (live.kind === "blocked")
    return result(name, "error", checks, "model_blocked");
  const sim = live.simulation;
  if (live.kind !== "graded")
    return result(name, live.kind, checks, live.reason, sim);
  const all = { ...checks, judge: live.check };
  const failing = live.check.verdicts.find((v) => !v.pass);
  return failing === undefined
    ? result(name, "passed", all, undefined, sim)
    : result(
        name,
        "failed",
        all,
        `judge: criterion ${failing.criterion} failed`,
        sim,
      );
}

const TEAM_LEAD: LiveOutcome = {
  kind: "skipped",
  reason: "live_not_runnable:team_calls",
  calls: { agent: 0, user: 0, judge: 0 },
  cost: null,
};

async function liveOf(
  plan: Plan,
  log: CaseLog,
  c: Parameters<typeof liveCheck>[0],
) {
  const started = log.events.find((e) => e.type === "thread_started");
  const name =
    started?.type === "thread_started" ? started.data.agent_name : "";
  const target = plan.agents.find((a) => a.name === name);
  if (plan.live === undefined || target === undefined) return undefined;
  const pinned = dryPin(target);
  // A lead's members run in the team worker, where the case's stubs can't reach: never live.
  if (pinned.leadsTeam) return TEAM_LEAD;
  const fresh = pinned.started.sandbox_provider !== undefined;
  return liveCheck(c, log, target, {
    live: plan.live,
    store: plan.store,
    kept: plan.kept,
    fresh,
  });
}

/** A case the free checks passed (or skipped), for the checks that come after. */
type Offline = {
  readonly c: CaseDir;
  readonly log: CaseLog;
  readonly checks: Checks;
  readonly skip: string | undefined;
};

/** Read, replay and rerun: a finished result when one of them ends the case. */
async function offline(plan: Plan, name: string): Promise<Offline | Evaluated> {
  const read = readCase(plan.root, name);
  if (!read.ok)
    return { result: result(name, "error", {}, `unreadable: ${read.error}`) };
  const c = read.value;
  const replayed = await replayCheck(c);
  const checks = { replay: replayed.check };
  if (replayed.log === undefined || !replayed.check.ok) {
    const code = replayed.check.ok ? "log_corrupt" : replayed.check.code;
    return { result: result(name, "failed", checks, `replay: ${code}`) };
  }
  const skip = skipReason(c, replayed.log);
  if (skip !== undefined) return { c, log: replayed.log, checks, skip };
  const rerun = await rerunCheck(c);
  if ("error" in rerun)
    return { result: result(name, "error", checks, rerun.error) };
  const all = { ...checks, rerun: rerun.check };
  return rerun.check.ok
    ? { c, log: replayed.log, checks: all, skip }
    : { result: result(name, "failed", all, rerunReason(rerun.check)) };
}

async function evaluate(plan: Plan, name: string): Promise<Evaluated> {
  const done = await offline(plan, name);
  if ("result" in done) return done;
  const { c, log, skip } = done;
  const drift =
    plan.pins === undefined ? undefined : driftCheck(c, log, plan.pins);
  const checks = drift === undefined ? done.checks : { ...done.checks, drift };
  const stale = drift?.ok === false ? driftReason(drift) : undefined;
  const live = await liveOf(plan, log, c);
  if (live !== undefined) {
    const judged = judgedStatus(name, checks, live);
    return judged.status === "passed" && stale !== undefined
      ? {
          result: result(
            name,
            "stale",
            judged.checks,
            stale,
            judged.simulation,
          ),
          live,
        }
      : { result: judged, live };
  }
  if (skip !== undefined)
    return { result: result(name, "skipped", checks, skip) };
  return stale === undefined
    ? { result: result(name, "passed", checks) }
    : { result: result(name, "stale", checks, stale) };
}

/** Runs every saved case's checks and returns the report (spec/api.json runEvals). */
export async function runEvals(
  options: RunEvalsOptions = {},
): Promise<EvalReport> {
  checkOptions(options);
  const root = options.cases ?? "cases";
  const agents = options.agents ?? [];
  const plan: Plan = {
    root,
    pins:
      options.agents === undefined ? undefined : agents.map((a) => dryPin(a)),
    agents,
    live: options.live,
    store: tenantStore(options.store ?? sqlite(":memory:"), "evals"),
    kept: options.store !== undefined,
  };
  const names = caseNames(root).filter(
    (n) => options.only === undefined || options.only.includes(n),
  );
  checkUser(root, names, options.live);
  return evalsOf(plan, names, options);
}

/** A model-kind simulated case needs live.user, checked before the first model call (32 E). */
function checkUser(
  root: string,
  names: readonly string[],
  live: Live | undefined,
): void {
  if (live === undefined || live.user !== undefined) return;
  for (const name of names) {
    const read = readCase(root, name);
    if (read.ok && read.value.meta.simulate?.kind === "model")
      throw new ConfigError(
        "invalid_config",
        `case ${name} simulates a user with a model: set live.user`,
      );
  }
}

/** The cases in order; a guard block stops the run and leaves the rest not_run. */
export async function evalsOf(
  plan: Plan,
  names: readonly string[],
  options: Pick<RunEvalsOptions, "strict">,
): Promise<EvalReport> {
  const cases: EvalCaseResult[] = [];
  const calls = { agent: 0, user: 0, judge: 0 };
  let cost: Cost | null | undefined;
  let aborted: NonNullable<EvalReport["aborted"]> | undefined;
  for (const name of names) {
    if (aborted !== undefined) {
      cases.push(result(name, "not_run", {}));
      continue;
    }
    const { result: r, live } = await evaluate(plan, name);
    cases.push(r);
    if (live?.kind === "blocked")
      aborted = { code: "model_blocked", case: name, model: live.model };
    else if (live !== undefined) {
      calls.agent += live.calls.agent;
      calls.user += live.calls.user;
      calls.judge += live.calls.judge;
      cost = addCost(cost, live.cost);
    }
  }
  return report(cases, {
    calls,
    cost: cost ?? null,
    live: plan.live !== undefined,
    agents: plan.pins !== undefined,
    strict: options.strict === true,
    ...(aborted === undefined ? {} : { aborted }),
  });
}

export type { Plan as EvalPlan };

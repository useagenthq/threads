import type { KnownEvent, Policy, Principal } from "../log";
import {
  type Agents,
  type Authorization,
  type ChildRun,
  FINAL_OUTPUT,
  type LoopConfig,
  type ToolImpl,
} from "../loop";
import { toolSpec } from "../loop/turn";
import type { Model } from "../model";
import { category, decide } from "../permissions";
import type { BudgetLedger } from "../store";
import { loopExtension } from "./extension";
import { extensionTools } from "./pin";
import { withMemoryWrite } from "./providers";
import { childFactory } from "./registry";
import type { ThreadRef } from "./result";
import type { Hooks, Resolved, RunOptions, SetUp } from "./run";
import { redactSecrets } from "./secret";

// The loop config for one run of an agent: its models, bound tools, hooks, permission fold and
// the agents it may start. A child's permission decisions are capped by its parent's.

const WORKSPACE = "/workspace";
const RANK = { allow: 0, ask: 1, deny: 2 } as const;

export type RunEnv<Deps> = {
  readonly options: RunOptions<Deps>;
  readonly principal: Principal;
  readonly thread: ThreadRef;
  readonly hooks: Hooks;
  readonly builtin: readonly ToolImpl[];
  readonly readFile?: (path: string) => Promise<Uint8Array | undefined>;
  readonly child?: ChildRun;
  readonly ledger: BudgetLedger;
  /** The branch's events, for the host's memory write authority. */
  readonly events: () => readonly KnownEvent[];
  /** The principal and host ceilings every decision is also made under. */
  readonly ceilings: readonly Permissions[];
};

type Permissions = NonNullable<Policy["permissions"]>;

export function loopConfig<Deps, Output>(
  def: SetUp<Deps, Output>,
  env: RunEnv<Deps>,
): LoopConfig {
  const { options, principal, thread, hooks, readFile } = env;
  const bindEnv = {
    deps: options.deps,
    threadId: thread.id,
    branchId: thread.branch,
    principal,
  };
  const models: readonly Model[] = [def.model, ...def.fallback];
  return {
    models: (ref) =>
      models.find(
        (m) =>
          m.info.model.provider === ref.provider &&
          m.info.model.name === ref.name,
      ),
    tools: new Map([
      ...env.builtin.map((t) => [t.spec.name, t] as const),
      ...[...def.bindable, ...extensionTools(def.hookable, def.mcp)].map(
        (t) => [t.name, t.bind(bindEnv)] as const,
      ),
    ]),
    extensions: def.hookable.map((e) =>
      loopExtension(e, (hc) => ({
        ...bindEnv,
        signal: hc.signal,
        ...(hc.callId === undefined ? {} : { callId: hc.callId }),
      })),
    ),
    authorize: narrowed(
      capped(
        (call, fold) =>
          withMemoryWrite(
            authorize(call, fold),
            def.memoryWrite,
            call.data.name,
            env.events(),
          ),
        env.ceilings,
      ),
      env.child,
    ),
    clock: {
      now: Date.now,
      sleepUntil: async (time) => {
        const { promise, resolve } = Promise.withResolvers<void>();
        setTimeout(resolve, Math.max(0, time - Date.now()));
        await promise;
      },
    },
    principal,
    skewMarginMs: 1000,
    redact: redactSecrets,
    agents: agents(def, env),
    budgets: { ledger: env.ledger, inherited: env.child?.covering ?? [] },
    ...(readFile === undefined ? {} : { readFile }),
    ...(def.output === undefined ? {} : { output: def.output }),
    ...(options.signal === undefined ? {} : { signal: options.signal }),
    ...(hooks.onEvent === undefined ? {} : { onEvent: hooks.onEvent }),
    ...(hooks.onDelta === undefined ? {} : { onDelta: hooks.onDelta }),
  };
}

const authorize: LoopConfig["authorize"] = (call, fold) => {
  const permissions = fold.policy?.permissions;
  if (permissions === undefined) return { decision: "ask", source: "default" };
  const spec = toolSpec(fold, call.data.name);
  const d = decide(permissions, WORKSPACE, {
    tool: call.data.name,
    category: category(call.data.name, spec?.effect_class),
    input: call.data.input,
    mode: fold.mode,
  });
  return {
    decision: d.decision,
    source: d.source,
    ...(d.rule === undefined ? {} : { rule_id: d.rule }),
  };
};

/**
 * Each ceiling decides the call too, in its own mode; the stricter wins and a tie reports the
 * thread's own decision.
 */
function capped(
  own: LoopConfig["authorize"],
  ceilings: readonly Permissions[],
): LoopConfig["authorize"] {
  if (ceilings.length === 0) return own;
  return (call, fold) =>
    ceilings.reduce(
      (decided: Authorization, ceiling) => {
        const d = decide(ceiling, WORKSPACE, {
          tool: call.data.name,
          category: category(
            call.data.name,
            toolSpec(fold, call.data.name)?.effect_class,
          ),
          input: call.data.input,
          mode: ceiling.mode,
        });
        return RANK[d.decision] > RANK[decided.decision]
          ? {
              decision: d.decision,
              source: d.source,
              ...(d.rule === undefined ? {} : { rule_id: d.rule }),
            }
          : decided;
      },
      own(call, fold),
    );
}

/** A child's decision is its own policy's, capped by its parent's: the stricter wins. */
function narrowed(
  own: LoopConfig["authorize"],
  child: ChildRun | undefined,
): LoopConfig["authorize"] {
  if (child === undefined) return own;
  return (call, fold): Authorization => {
    const mine = own(call, fold);
    // The child's own structured result touches only its log; the parent has no such tool.
    if (call.data.name === FINAL_OUTPUT) return mine;
    const ceiling = child.ceiling(call);
    return RANK[ceiling.decision] > RANK[mine.decision] ? ceiling : mine;
  };
}

function agents<Deps, Output>(
  def: Resolved<Deps, Output>,
  env: RunEnv<Deps>,
): Agents {
  const { signal } = env.options;
  const shared = {
    store: env.thread.store,
    principal: env.principal,
    ...(signal === undefined ? {} : { signal }),
  };
  return {
    name: def.name,
    subagent: (name) => {
      const found = def.agents.find((a) => a.name === name);
      return found === undefined ? undefined : childFactory(found)?.(shared);
    },
    ...(env.child === undefined ? {} : { team: env.child.team }),
  };
}

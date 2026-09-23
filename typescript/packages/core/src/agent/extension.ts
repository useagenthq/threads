import type { EventOf } from "../fold/state";
import type { Observer, ObserverHandler } from "../hooks/observers";
import type {
  HookContext,
  HookName,
  HookReturns,
  LoopExtension,
  LoopHooks,
  SessionSource,
} from "../hooks/types";
import { type KnownEvent, Name } from "../log";
import type { RunErrorCode } from "../loop";
import type { ReducedState } from "../reduce";
import { ConfigError } from "./errors";
import type { RunContext, Tool } from "./tool";

// extension() (spec/api.json): the one primitive for tools, trusted
// instructions, hooks and observers. Trusted host code, not a security boundary. What it pins
// (name, instructions, tools, which hooks and observers exist, the timeout) is part of the
// thread's config_hash, so no agent path and no later run can change a thread's hooks.

type Call = EventOf<"tool_call">["data"];
type Result = EventOf<"tool_result">["data"];
type Response = EventOf<"model_response" | "model_response_recovered">["data"];
type Ctx<Deps> = RunContext<Deps>;
type Settings = EventOf<"settings_changed">["data"]["settings"];

/** spec/api.json types.Hooks, in TS casing. Every return is a wire hook_decision value. */
export type Hooks<Deps = undefined> = {
  readonly sessionStart?: (
    source: SessionSource,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["session_start"]>;
  readonly sessionEnd?: (ctx: Ctx<Deps>) => Promise<void>;
  readonly beforeInput?: (
    input: EventOf<"user_input">["data"],
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["before_input"]>;
  readonly beforeModel?: (
    state: ReducedState,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["before_model"]>;
  readonly afterModel?: (
    state: ReducedState,
    response: Response,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["after_model"]>;
  readonly beforeTool?: (
    call: Call,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["before_tool"]>;
  readonly permissionRequest?: (
    call: Call,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["permission_request"]>;
  readonly permissionDenied?: (call: Call, ctx: Ctx<Deps>) => Promise<void>;
  readonly afterTool?: (
    call: Call,
    result: Result,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["after_tool"]>;
  readonly beforeToolResult?: (
    call: Call,
    result: Result,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["before_tool_result"]>;
  readonly afterToolBatch?: (
    state: ReducedState,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["after_tool_batch"]>;
  readonly beforeCompact?: (
    state: ReducedState,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["before_compact"]>;
  readonly afterCompact?: (
    state: ReducedState,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["after_compact"]>;
  readonly onStop?: (
    state: ReducedState,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["on_stop"]>;
  readonly onStopFailure?: (
    code: RunErrorCode,
    ctx: Ctx<Deps>,
  ) => Promise<void>;
  readonly subagentStart?: (
    call: Call,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["subagent_start"]>;
  readonly subagentStop?: (
    finished: EventOf<"agent_finished">["data"],
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["subagent_stop"]>;
  readonly beforeModelSwitch?: (
    settings: Settings,
    ctx: Ctx<Deps>,
  ) => Promise<HookReturns["before_model_switch"]>;
  readonly afterModelSwitch?: (
    settings: Settings,
    ctx: Ctx<Deps>,
  ) => Promise<void>;
  readonly notification?: (event: KnownEvent, ctx: Ctx<Deps>) => Promise<void>;
};

export type ExtensionOptions<Deps = undefined> = {
  /** Unique; its tools are namespaced `<name>__<tool>`. */
  readonly name: string;
  readonly tools?: readonly Tool<unknown, unknown, Deps>[];
  /** Static; line 0 after the base instructions, in declaration order. */
  readonly instructions?: string;
  readonly hooks?: Hooks<Deps>;
  /** Observers keyed by event type, or "*": committed events after append. */
  readonly on?: Readonly<Record<string, ObserverHandler>>;
  /** Runs once at check() or the first run. Throwing is a ConfigError. */
  readonly setup?: () => Promise<void>;
  /** Per hook call; default 5 s. */
  readonly hookTimeoutMs?: number;
};

export type Extension<Deps = undefined> = ExtensionOptions<Deps> & {
  readonly kind: "extension";
};

const DEFAULT_TIMEOUT_MS = 5000;

export function extension<Deps = undefined>(
  options: ExtensionOptions<Deps>,
): Extension<Deps> {
  if (!Name.safeParse(options.name).success)
    throw new ConfigError(
      "invalid_config",
      `extension name ${JSON.stringify(options.name)} is not a valid name`,
    );
  const timeout = options.hookTimeoutMs;
  if (timeout !== undefined && (!Number.isInteger(timeout) || timeout <= 0))
    throw new ConfigError(
      "invalid_config",
      `extension ${options.name}: hookTimeoutMs must be a positive integer`,
    );
  return { ...options, kind: "extension" };
}

/** The wire name of every hook an extension defines, in the order of the wire enum. */
export function hookNames<Deps>(ext: Extension<Deps>): readonly HookName[] {
  return Object.keys(bindHooks(ext, () => undefined)).filter(isHookName);
}

const HOOK_NAMES: ReadonlySet<string> = new Set<HookName>([
  "session_start",
  "session_end",
  "before_input",
  "before_model",
  "after_model",
  "before_tool",
  "permission_request",
  "permission_denied",
  "after_tool",
  "before_tool_result",
  "after_tool_batch",
  "before_compact",
  "after_compact",
  "on_stop",
  "stop_failure",
  "subagent_start",
  "subagent_stop",
  "before_model_switch",
  "after_model_switch",
  "notification",
]);

const isHookName = (name: string): name is HookName => HOOK_NAMES.has(name);

/** The loop's view of an extension, bound to this run's context. */
export function loopExtension<Deps>(
  ext: Extension<Deps>,
  ctx: (hook: HookContext) => Ctx<Deps>,
): LoopExtension {
  return {
    name: ext.name,
    timeoutMs: ext.hookTimeoutMs ?? DEFAULT_TIMEOUT_MS,
    hooks: bindHooks(ext, ctx),
  };
}

export function observerOf<Deps>(ext: Extension<Deps>): Observer | undefined {
  return ext.on === undefined ? undefined : { name: ext.name, on: ext.on };
}

type Bind<Deps> = (hook: HookContext) => Ctx<Deps> | undefined;

/** Each camelCase hook as the loop calls it: wire name, args tuple, the run context bound. */
function bindHooks<Deps>(ext: Extension<Deps>, bind: Bind<Deps>): LoopHooks {
  const h = ext.hooks ?? {};
  const c = (hc: HookContext): Ctx<Deps> => {
    const bound = bind(hc);
    if (bound === undefined) throw new Error("hook names only, not callable");
    return bound;
  };
  const one = <K extends HookName>(
    name: K,
    fn: NonNullable<LoopHooks[K]> | undefined,
  ): LoopHooks => (fn === undefined ? {} : { [name]: fn });
  const {
    sessionStart,
    sessionEnd,
    beforeInput,
    beforeModel,
    afterModel,
    beforeTool,
    permissionRequest,
    permissionDenied,
    afterTool,
    beforeToolResult,
    afterToolBatch,
    beforeCompact,
    afterCompact,
    onStop,
    onStopFailure,
    subagentStart,
    subagentStop,
    beforeModelSwitch,
    afterModelSwitch,
    notification,
  } = h;
  const opt = <F, K extends HookName>(
    fn: F | undefined,
    name: K,
    wrap: (f: F) => NonNullable<LoopHooks[K]>,
  ): LoopHooks => one(name, fn === undefined ? undefined : wrap(fn));
  return {
    ...opt(
      sessionStart,
      "session_start",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(sessionEnd, "session_end", (f) => (_, x) => f(c(x))),
    ...opt(
      beforeInput,
      "before_input",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      beforeModel,
      "before_model",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      afterModel,
      "after_model",
      (f) =>
        ([a, b], x) =>
          f(a, b, c(x)),
    ),
    ...opt(
      beforeTool,
      "before_tool",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      permissionRequest,
      "permission_request",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      permissionDenied,
      "permission_denied",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      afterTool,
      "after_tool",
      (f) =>
        ([a, b], x) =>
          f(a, b, c(x)),
    ),
    ...opt(
      beforeToolResult,
      "before_tool_result",
      (f) =>
        ([a, b], x) =>
          f(a, b, c(x)),
    ),
    ...opt(
      afterToolBatch,
      "after_tool_batch",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      beforeCompact,
      "before_compact",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      afterCompact,
      "after_compact",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      onStop,
      "on_stop",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      onStopFailure,
      "stop_failure",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      subagentStart,
      "subagent_start",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      subagentStop,
      "subagent_stop",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      beforeModelSwitch,
      "before_model_switch",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      afterModelSwitch,
      "after_model_switch",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
    ...opt(
      notification,
      "notification",
      (f) =>
        ([a], x) =>
          f(a, c(x)),
    ),
  };
}

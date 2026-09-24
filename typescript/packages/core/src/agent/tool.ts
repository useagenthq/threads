import { z } from "zod";
import {
  canonicalize,
  type EffectClass,
  JsonObject,
  JsonValue,
  type Principal,
  ToolSpec,
} from "../log";
import type { ToolImpl, ToolRun } from "../loop";
import type { LookupResult } from "../model";
import { ConfigError } from "./errors";

// tool() (spec/api.json): an app tool. Its Zod input schema is both the JSON Schema the model
// sees and the parser its arguments go through.

/** What tools and hooks receive (spec/api.json RunContext). Deps are host-only. */
export type RunContext<Deps = undefined> = {
  readonly deps: Deps | undefined;
  readonly threadId: string;
  readonly branchId: string;
  readonly principal: Principal;
  readonly callId?: string;
  /** `<branch_id>:<call_id>`: send it to providers that dedup. */
  readonly effectKey?: string;
  readonly signal: AbortSignal;
};

export type ToolDefinition<Input, Output, Deps = undefined> = {
  readonly name: string;
  readonly description: string;
  readonly input: z.ZodType<Input>;
  readonly output?: z.ZodType<Output>;
  /** Defaults to "host". "sandbox" is reserved: setup fails with capability_missing. Not pinned. */
  readonly runs?: "host" | "sandbox";
  readonly execute?: (input: Input, ctx: RunContext<Deps>) => Promise<Output>;
  /**. Undeclared tools are unguarded: uncertainty always parks. */
  readonly effect?: z.infer<typeof EffectClass>;
  readonly dedupWindowMs?: number;
  readonly reconcile?: {
    readonly lookup: (
      effectKey: string,
      ctx: RunContext<Deps>,
    ) => Promise<LookupResult<Output>>;
    readonly finality: "final" | "nonfinal";
  };
  readonly endsTurn?: boolean;
  /** true: runs with the other concurrent read-only calls of one response. Hashed, not in line 0. */
  readonly concurrent?: boolean;
  /** true: the model sees only its name, listed in tool_search's description, until tool_search loads it. */
  readonly defer?: boolean;
};

/** The environment a run binds its tools to. */
export type ToolEnv<Deps> = {
  readonly deps: Deps | undefined;
  readonly threadId: string;
  readonly branchId: string;
  readonly principal: Principal;
};

/** An app tool. `types` only carries Input and Output for inference. */
export type Tool<Input, Output, Deps = undefined> = {
  readonly name: string;
  readonly types?: { readonly input: Input; readonly output: Output };
  /** The pinned spec; throws ConfigError when the definition can't run. */
  readonly spec: () => ToolSpec;
  readonly bind: (env: ToolEnv<Deps>) => ToolImpl;
  /** Declared `concurrent: true`; pinned by config_hash as concurrent_tools. */
  readonly concurrent?: true;
  /** Declared `defer: true`: pinned in reference form when context.defer_tools is auto. */
  readonly defer?: true;
};

export function tool<Input, Output, Deps = undefined>(
  def: ToolDefinition<Input, Output, Deps>,
): Tool<Input, Output, Deps> {
  // Arguments must match the pinned schema exactly: an object schema refuses unknown keys.
  const strict: z.ZodType =
    def.input instanceof z.ZodObject ? def.input.strict() : def.input;
  const spec = (): ToolSpec => pinned(def, strict);
  return {
    name: def.name,
    spec,
    bind: (env) => bound(def, strict, spec(), env),
    ...(def.concurrent === true ? { concurrent: true } : {}),
    ...(def.defer === true ? { defer: true } : {}),
  };
}

function pinned<Input, Output, Deps>(
  def: ToolDefinition<Input, Output, Deps>,
  input: z.ZodType,
): ToolSpec {
  const effect = def.effect ?? "unguarded";
  if ((def.runs ?? "host") !== "host" || def.execute === undefined)
    throw new ConfigError(
      "capability_missing",
      `tool ${def.name}: only runs "host" with execute is supported in this release`,
    );
  if ((effect === "idempotent") !== (def.dedupWindowMs !== undefined))
    throw new ConfigError(
      "invalid_config",
      `tool ${def.name}: dedupWindowMs is required exactly when effect is idempotent`,
    );
  if ((effect === "reconcilable") !== (def.reconcile !== undefined))
    throw new ConfigError(
      "invalid_config",
      `tool ${def.name}: reconcile is required exactly when effect is reconcilable`,
    );
  if (
    def.concurrent === true &&
    (effect !== "read_only" || def.endsTurn === true)
  )
    throw new ConfigError(
      "invalid_config",
      `tool ${def.name}: concurrent needs effect: "read_only" and no endsTurn; tools with side effects, or that end the turn, run one at a time`,
    );
  if (def.defer === true && def.endsTurn === true)
    throw new ConfigError(
      "invalid_config",
      `tool ${def.name}: defer can't be combined with endsTurn; a deferred tool is loaded by tool_search first`,
    );
  const parsed = ToolSpec.safeParse({
    name: def.name,
    description: def.description,
    input_schema: jsonSchema(def.name, input),
    effect_class: effect,
    ...(def.dedupWindowMs === undefined
      ? {}
      : { dedup_window_ms: def.dedupWindowMs }),
    ...(def.output === undefined
      ? {}
      : { output_schema: jsonSchema(def.name, def.output) }),
    ...(def.endsTurn === true ? { ends_turn: true } : {}),
  });
  if (!parsed.success)
    throw new ConfigError(
      "invalid_config",
      `tool ${def.name}: ${z.prettifyError(parsed.error)}`,
    );
  return parsed.data;
}

/** The exported JSON Schema: the one source the model sees (AGENTS.md, Design idea). */
export function jsonSchema(
  name: string,
  schema: z.ZodType,
): z.infer<typeof JsonObject> {
  let exported: unknown;
  try {
    exported = z.toJSONSchema(schema, { io: "input" });
  } catch (error) {
    throw new ConfigError("invalid_config", `${name}: ${String(error)}`);
  }
  const { $schema: _dialect, ...rest } = JsonObject.parse(exported);
  return rest;
}

function bound<Input, Output, Deps>(
  def: ToolDefinition<Input, Output, Deps>,
  strict: z.ZodType,
  spec: ToolSpec,
  env: ToolEnv<Deps>,
): ToolImpl {
  const context = (
    callId: string | undefined,
    effectKey: string | undefined,
    signal: AbortSignal,
  ): RunContext<Deps> => ({
    ...env,
    signal,
    ...(callId === undefined ? {} : { callId }),
    ...(effectKey === undefined ? {} : { effectKey }),
  });
  const run = async (
    args: unknown,
    ctx: {
      readonly callId: string;
      readonly effectKey: string;
      readonly signal: AbortSignal;
    },
  ): Promise<ToolRun> => {
    const execute = def.execute;
    if (execute === undefined)
      throw new Error(`tool ${def.name} has no execute`);
    // The loop parsed the arguments with `strict` already; this gives execute its typed Input.
    const parsed = def.input.parse(args);
    try {
      return done(
        await execute(parsed, context(ctx.callId, ctx.effectKey, ctx.signal)),
        def.output,
      );
    } catch (error) {
      // spec/api.json choice tool-errors: a thrown error is a result the model sees.
      return { kind: "done", output: String(error), isError: true };
    }
  };
  const reconcile = def.reconcile;
  return {
    spec,
    input: strict,
    run,
    ...(def.concurrent === true ? { concurrent: true } : {}),
    ...(reconcile === undefined
      ? {}
      : {
          reconcile: {
            finality: reconcile.finality,
            lookup: async (key: string): Promise<LookupResult<string>> => {
              const answer = await reconcile.lookup(
                key,
                context(undefined, key, new AbortController().signal),
              );
              if (answer.status !== "found") return answer;
              const shown = done(answer.value, def.output);
              return shown.kind === "done" && !shown.isError
                ? { status: "found", value: shown.output }
                : {
                    status: "unknown",
                    reason: "the found value fails the output schema",
                  };
            },
          },
        }),
  };
}

/** The model-visible text of a value: a string as is, anything else as canonical JSON. */
function done<Output>(
  value: Output,
  schema: z.ZodType<Output> | undefined,
): ToolRun {
  if (schema !== undefined && !schema.safeParse(value).success)
    return {
      kind: "done",
      output: "the result fails the tool's output schema",
      isError: true,
    };
  if (typeof value === "string")
    return { kind: "done", output: value, isError: false };
  const json = JsonValue.safeParse(value);
  const text = json.success ? canonicalize(json.data) : undefined;
  return text?.ok === true
    ? { kind: "done", output: text.value, isError: false }
    : { kind: "done", output: "the result is not JSON", isError: true };
}

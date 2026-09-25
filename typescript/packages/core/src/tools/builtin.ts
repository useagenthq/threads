import type { z } from "zod";
import { jsonSchema } from "../agent/tool";
import { assertNever } from "../assert-never";
import { type EffectClass, type KnownEvent, ToolSpec } from "../log";
import type { ToolContext, ToolImpl, ToolRun } from "../loop/types";
import { err, type Result } from "../result";
import type {
  Failure,
  FileFailure,
  SandboxContext,
  SandboxSession,
  Stale,
} from "../sandbox/protocol";
import type { ArtifactStore } from "../store/artifacts";
import { entry } from "./catalog";

// A built-in tool: a pinned spec, and a body bound to one run's sandbox session,
// artifacts and log. Arguments are parsed with the same Zod schema the spec exports.

export type SessionFailure = Failure<"unavailable" | "timeout"> | Stale;

export type BuiltinEnv = {
  /** The run's sandbox session, created (through the resource ledger) on first use. */
  readonly session: () => Promise<Result<SandboxSession, SessionFailure>>;
  readonly context: SandboxContext;
  readonly artifacts: ArtifactStore;
  readonly events: () => readonly KnownEvent[];
};

export type Builtin = {
  readonly spec: ToolSpec;
  readonly bind: (env: BuiltinEnv) => ToolImpl;
};

type Def<S extends z.ZodType> = {
  /** A catalog name; its description and input schema come from the catalog. */
  readonly name: string;
  readonly input: S;
  readonly effect: z.infer<typeof EffectClass>;
  readonly run: (
    input: z.infer<S>,
    ctx: ToolContext,
    env: BuiltinEnv,
  ) => Promise<ToolRun>;
  readonly terminate?: (env: BuiltinEnv) => NonNullable<ToolImpl["terminate"]>;
  /** reconcilable: the lookup that settles an unknown call. */
  readonly reconcile?: (env: BuiltinEnv) => NonNullable<ToolImpl["reconcile"]>;
};

export function builtin<S extends z.ZodType>(def: Def<S>): Builtin {
  const listed = entry(def.name);
  if (listed.input !== def.input)
    throw new Error(`built-in ${def.name} must use its catalog input schema`);
  const spec = ToolSpec.parse({
    name: def.name,
    description: listed.description,
    input_schema: jsonSchema(def.name, def.input),
    effect_class: def.effect,
  });
  return {
    spec,
    bind: (env) => {
      const terminate = def.terminate?.(env);
      const reconcile = def.reconcile?.(env);
      return {
        spec,
        input: def.input,
        run: async (input, ctx) => def.run(def.input.parse(input), ctx, env),
        ...(terminate === undefined ? {} : { terminate }),
        ...(reconcile === undefined ? {} : { reconcile }),
      };
    },
  };
}

/**
 * A sandbox_local built-in under open egress: what it changes may reach outside the sandbox, so
 * it is unguarded and an in-doubt call parks. Any other built-in is returned as it is.
 */
export function unguarded(b: Builtin): Builtin {
  if (b.spec.effect_class !== "sandbox_local") return b;
  const spec = ToolSpec.parse({ ...b.spec, effect_class: "unguarded" });
  return { spec, bind: (env) => ({ ...b.bind(env), spec }) };
}

export const done = (output: string, isError = false): ToolRun => ({
  kind: "done",
  output,
  isError,
});

/** A provider failure as a run: after dispatch, unavailable is uncertain; a fence sent nothing. */
export function failed(error: FileFailure | SessionFailure): ToolRun {
  switch (error.code) {
    case "stale_epoch":
    case "cleanup_claim_lost":
      return { kind: "not_sent" };
    case "unavailable":
    case "timeout":
      return {
        kind: "unknown",
        reason: error.code === "timeout" ? "timeout" : "transport_error",
      };
    case "not_found":
    case "invalid_path":
    case "permission_denied":
    case "is_directory":
    case "too_large":
      return done(`${error.code}: ${error.message}`, true);
    default:
      return assertNever(error);
  }
}

/** The session, or the run that says it was never reached: no sandbox, nothing sent. */
export async function sessionOf(
  env: BuiltinEnv,
): Promise<Result<SandboxSession, ToolRun>> {
  const got = await env.session();
  return got.ok ? got : err({ kind: "not_sent" });
}

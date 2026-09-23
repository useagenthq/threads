import { posix } from "node:path";
import { z } from "zod";
import type { ToolRun } from "../loop/types";
import { globMatches } from "../permissions/match";
import { type ExecResult, execute, toolRunOf } from "../sandbox/exec";
import {
  type Builtin,
  type BuiltinEnv,
  builtin,
  done,
  sessionOf,
} from "./builtin";

// glob and grep: read_only listings built on exec of POSIX find and grep, so every
// provider answers the same way. Results are capped here and spilled like any result (L0).

const WORKSPACE = "/workspace";
/** Bytes of listing kept in memory per stream; the rest stays in the exec's artifact. */
// ponytail: a listing past 1 MiB is cut to its head and tail; page with a narrower path.
const LISTING_BYTES = 1 << 20;

const Path = z
  .string()
  .min(1)
  .describe("Directory to search, relative to /workspace (use . for all).");

async function run(
  env: BuiltinEnv,
  command: readonly string[],
  processKey: string,
): Promise<ExecResult | ToolRun> {
  const session = await sessionOf(env);
  if (!session.ok) return session.error;
  const result = await execute(
    session.value,
    command,
    env.context,
    { env: {}, processKey },
    env.artifacts,
    LISTING_BYTES,
  );
  return result.ok ? result.value : toolRunOf(result);
}

const isRun = (r: ExecResult | ToolRun): r is ToolRun => "kind" in r;

export const glob: Builtin = builtin({
  name: "glob",
  description:
    "List files under path whose path relative to it matches a gitignore-style glob (** spans directories).",
  input: z.strictObject({ pattern: z.string().min(1), path: Path }),
  effect: "read_only",
  run: async ({ pattern, path }, ctx, env) => {
    const root = posix.resolve(WORKSPACE, path);
    const found = await run(env, ["find", root, "-type", "f"], ctx.effectKey);
    if (isRun(found)) return found;
    if (found.exit_code !== 0) return done(found.stderr, true);
    const hits = found.stdout
      .split("\n")
      .filter((p) => p !== "" && globMatches(pattern, posix.relative(root, p)));
    return done(hits.length === 0 ? "no files match" : hits.join("\n"));
  },
});

export const grep: Builtin = builtin({
  name: "grep",
  description:
    "Search file contents under path with an extended regular expression; prints path:line:text.",
  input: z.strictObject({ pattern: z.string().min(1), path: Path }),
  effect: "read_only",
  run: async ({ pattern, path }, ctx, env) => {
    const root = posix.resolve(WORKSPACE, path);
    const command = ["grep", "-rnIE", "-e", pattern, "--", root];
    const found = await run(env, command, ctx.effectKey);
    if (isRun(found)) return found;
    // grep exits 1 for no match and 2 or more for an error.
    if (found.exit_code === 1) return done("no matches");
    if (found.exit_code !== 0) return done(found.stderr, true);
    return done(found.stdout);
  },
});

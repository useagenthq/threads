import { posix } from "node:path";
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
import { GlobInput, GrepInput, LsInput } from "./catalog";

// glob and grep: read_only listings built on exec of POSIX find and grep, so every
// provider answers the same way. Results are capped here and spilled like any result (L0).

const WORKSPACE = "/workspace";
/** Bytes of listing kept in memory per stream; the rest stays in the exec's artifact. */
// ponytail: a listing past 1 MiB is cut to its head and tail; page with a narrower path.
const LISTING_BYTES = 1 << 20;

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
const failedRun = (r: readonly string[] | ToolRun): r is ToolRun =>
  !Array.isArray(r);

/** Files under `root` whose path relative to it matches `pattern`, or the run that failed. */
async function matching(
  env: BuiltinEnv,
  root: string,
  pattern: string,
  processKey: string,
): Promise<readonly string[] | ToolRun> {
  const found = await run(env, ["find", root, "-type", "f"], processKey);
  if (isRun(found)) return found;
  if (found.exit_code !== 0) return done(found.stderr, true);
  return found.stdout
    .split("\n")
    .filter((p) => p !== "" && globMatches(pattern, posix.relative(root, p)));
}

export const glob: Builtin = builtin({
  name: "glob",
  input: GlobInput,
  effect: "read_only",
  run: async ({ pattern, path }, ctx, env) => {
    const hits = await matching(
      env,
      posix.resolve(WORKSPACE, path),
      pattern,
      ctx.effectKey,
    );
    if (failedRun(hits)) return hits;
    return done(hits.length === 0 ? "no files match" : hits.join("\n"));
  },
});

export const ls: Builtin = builtin({
  name: "ls",
  input: LsInput,
  effect: "read_only",
  run: async ({ path }, ctx, env) => {
    const dir = posix.resolve(WORKSPACE, path);
    const found = await run(env, ["ls", "-1Ap", "--", dir], ctx.effectKey);
    if (isRun(found)) return found;
    if (found.exit_code !== 0) return done(found.stderr, true);
    return done(found.stdout === "" ? "empty directory" : found.stdout);
  },
});

export const grep: Builtin = builtin({
  name: "grep",
  input: GrepInput,
  effect: "read_only",
  run: async ({ pattern, path, glob: only }, ctx, env) => {
    const root = posix.resolve(WORKSPACE, path);
    const files =
      only === undefined
        ? undefined
        : await matching(env, root, only, ctx.effectKey);
    if (files !== undefined && failedRun(files)) return files;
    const command = ["grep", "-rnIE", "-e", pattern, "--", root];
    const found = await run(env, command, ctx.effectKey);
    if (isRun(found)) return found;
    // grep exits 1 for no match and 2 or more for an error.
    if (found.exit_code === 1) return done("no matches");
    if (found.exit_code !== 0) return done(found.stderr, true);
    const lines = found.stdout
      .split("\n")
      .filter(
        (l) =>
          l !== "" &&
          (files === undefined || files.some((f) => l.startsWith(`${f}:`))),
      );
    return done(lines.length === 0 ? "no matches" : lines.join("\n"));
  },
});

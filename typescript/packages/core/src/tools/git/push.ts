import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { z } from "zod";
import type { ToolContext, ToolRun } from "../../loop/types";
import type { LookupResult } from "../../model";
import { err, ok, type Result } from "../../result";
import {
  type Builtin,
  type BuiltinEnv,
  builtin,
  done,
  failed,
} from "../builtin";
import { GitPushInput } from "../gateway-inputs";
import { missingCredential } from "./clone";
import { cloneDir, type GitOptions, remoteUrl, safeRef, scratch } from "./host";
import { BUNDLES, type Sandboxed, sandboxed } from "./sandbox";

// git_push: reconcilable. The sandbox bundles the branch; the host verifies
// the bundle's ref against the sandbox's head, then pushes with the credential (never force).
// A push that may have landed is settled by the remote ref: found when it equals the pushed
// commit. Anything else stays unknown and parks: a push is never repeated blindly.

type Input = z.infer<typeof GitPushInput>;
type Target = { readonly dir: string; readonly ref: string };

function target(o: GitOptions, input: Input): Result<Target, ToolRun> {
  const missing = missingCredential(o);
  if (missing !== undefined) return err(missing);
  if (!safeRef(input.branch))
    return err(done(`branch ${input.branch} is not a plain branch name`, true));
  const dir = cloneDir(input.repo, input.path);
  if (dir === undefined)
    return err(done("path must stay inside /workspace", true));
  return ok({ dir, ref: `refs/heads/${input.branch}` });
}

async function headOf(
  s: Sandboxed,
  t: Target,
): Promise<Result<string, ToolRun>> {
  const sha = await s.git(
    "-C",
    t.dir,
    "rev-parse",
    "--verify",
    `${t.ref}^{commit}`,
  );
  return sha.ok ? ok(sha.value.trim()) : sha;
}

/** The commit the forge has at the branch, "" when the branch is absent, or undefined. */
async function remoteHead(
  o: GitOptions,
  repo: string,
  ref: string,
): Promise<string | undefined> {
  return scratch(o, async (git) => {
    const listed = await git("ls-remote", remoteUrl(o, repo), ref);
    return listed.code === 0 ? (listed.stdout.split(/\s/)[0] ?? "") : undefined;
  });
}

async function pushBundle(
  o: GitOptions,
  input: Input,
  t: Target,
  sha: string,
  bytes: Uint8Array,
  ctx: ToolContext,
): Promise<ToolRun> {
  return scratch(o, async (git, dir) => {
    await writeFile(join(dir, "b.bundle"), bytes);
    const steps = [
      ["init", "--quiet", "--bare", "r.git"],
      ["-C", "r.git", "fetch", "--quiet", "../b.bundle", `${t.ref}:${t.ref}`],
    ];
    for (const args of steps) {
      const ran = await git(...args);
      if (ran.code !== 0)
        return done(`the branch bundle is unusable: ${ran.stderr}`, true);
    }
    const got = await git("-C", "r.git", "rev-parse", t.ref);
    if (got.stdout.trim() !== sha)
      return done(
        "the bundle's branch is not the sandbox's head; nothing pushed",
        true,
      );
    const fenced = await ctx.fence();
    if (!fenced.ok) return { kind: "not_sent" };
    const pushed = await git(
      "-C",
      "r.git",
      "push",
      "--quiet",
      remoteUrl(o, input.repo),
      `${t.ref}:${t.ref}`,
    );
    if (pushed.code === 0)
      return {
        kind: "done",
        output: `pushed ${sha} to ${input.repo} ${input.branch}`,
        isError: false,
        receipt: sha,
      };
    // The forge answered no: nothing changed.
    if (/\[(remote )?rejected\]/.test(pushed.stderr))
      return done(`push rejected: ${pushed.stderr}`, true);
    return { kind: "unknown", reason: "transport_error" };
  });
}

async function lookup(
  o: GitOptions,
  env: BuiltinEnv,
  effectKey: string,
  raw: unknown,
): Promise<LookupResult<string>> {
  const input = GitPushInput.safeParse(raw);
  if (!input.success)
    return { status: "unknown", reason: "the call's input is not git_push's" };
  const t = target(o, input.data);
  if (!t.ok)
    return { status: "unknown", reason: "the push target is not usable" };
  const s = await sandboxed(env, `${effectKey}:lookup`);
  if (!s.ok) return { status: "unknown", reason: "the sandbox is unavailable" };
  const sha = await headOf(s.value, t.value);
  const remote = await remoteHead(o, input.data.repo, t.value.ref);
  if (!sha.ok || remote === undefined)
    return { status: "unknown", reason: "the heads could not be read" };
  return remote === sha.value
    ? {
        status: "found",
        value: `pushed ${sha.value} to ${input.data.repo} ${input.data.branch}`,
      }
    : { status: "not_found" };
}

export function gitPush(o: GitOptions): Builtin {
  return builtin({
    name: "git_push",
    input: GitPushInput,
    effect: "reconcilable",
    run: async (input, ctx, env) => {
      const t = target(o, input);
      if (!t.ok) return t.error;
      const s = await sandboxed(env, ctx.effectKey);
      if (!s.ok) return s.error;
      const sha = await headOf(s.value, t.value);
      if (!sha.ok) return sha.error;
      const bundle = `${BUNDLES}/${ctx.callId}.bundle`;
      const made = await s.value.git(
        "-C",
        t.value.dir,
        "bundle",
        "create",
        bundle,
        t.value.ref,
      );
      if (!made.ok) return made.error;
      const bytes = await s.value.session.download(bundle, env.context);
      if (!bytes.ok) return failed(bytes.error);
      return pushBundle(o, input, t.value, sha.value, bytes.value, ctx);
    },
    // not_found is nonfinal: the remote may lag, or the head may have moved since. It parks.
    reconcile: (env) => ({
      finality: "nonfinal",
      lookup: (key, input) => lookup(o, env, key, input),
    }),
  });
}

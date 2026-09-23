import { readFile } from "node:fs/promises";
import { join } from "node:path";
import type { ToolContext, ToolRun } from "../../loop/types";
import { err, ok, type Result } from "../../result";
import { type Builtin, type BuiltinEnv, builtin, done } from "../builtin";
import { GitCloneInput, GitFetchInput } from "../gateway-inputs";
import { cloneDir, type GitOptions, remoteUrl, safeRef, scratch } from "./host";
import { BUNDLES, sandboxed, upload } from "./sandbox";

// git_clone and git_fetch: read_only toward the forge. The host mirrors the
// repository with the credential and bundles it; the sandbox clones or fetches from the bundle
// and keeps a credential-free origin URL.

/** Why a credential can't be used, before anything is sent. */
export function missingCredential(o: GitOptions): ToolRun | undefined {
  try {
    o.credential.reveal();
    return undefined;
  } catch (error) {
    return done(`missing_secret: ${String(error)}`, true);
  }
}

/** The repository mirrored on the host and bundled, fenced at the network send. */
async function bundled(
  o: GitOptions,
  repo: string,
  ctx: ToolContext,
): Promise<Result<Uint8Array, ToolRun>> {
  return scratch(o, async (git, dir) => {
    const fenced = await ctx.fence();
    if (!fenced.ok) return err({ kind: "not_sent" });
    const mirror = await git(
      "clone",
      "--mirror",
      "--quiet",
      remoteUrl(o, repo),
      "m.git",
    );
    if (mirror.code !== 0)
      return err(done(`git clone ${repo} failed: ${mirror.stderr}`, true));
    const made = await git(
      "-C",
      "m.git",
      "bundle",
      "create",
      "../b.bundle",
      "--all",
    );
    if (made.code !== 0)
      return err(done(`bundling ${repo} failed: ${made.stderr}`, true));
    return ok(new Uint8Array(await readFile(join(dir, "b.bundle"))));
  });
}

type Checked = { readonly dir: string; readonly bundle: string };

function checked(
  o: GitOptions,
  input: {
    readonly repo: string;
    readonly ref?: string | undefined;
    readonly path?: string | undefined;
  },
  ctx: ToolContext,
): Result<Checked, ToolRun> {
  const missing = missingCredential(o);
  if (missing !== undefined) return err(missing);
  if (input.ref !== undefined && !safeRef(input.ref))
    return err(
      done(`ref ${input.ref} is not a plain branch, tag or commit name`, true),
    );
  const dir = cloneDir(input.repo, input.path);
  if (dir === undefined)
    return err(done("path must stay inside /workspace", true));
  return ok({ dir, bundle: `${BUNDLES}/${ctx.callId}.bundle` });
}

async function steps(
  env: BuiltinEnv,
  ctx: ToolContext,
  bytes: Uint8Array,
  bundle: string,
  commands: readonly (readonly string[])[],
): Promise<ToolRun | undefined> {
  const s = await sandboxed(env, ctx.effectKey);
  if (!s.ok) return s.error;
  const put = await upload(s.value, env, bundle, bytes);
  if (!put.ok) return put.error;
  for (const args of commands) {
    const ran = await s.value.git(...args);
    if (!ran.ok) return ran.error;
  }
  return undefined;
}

export function gitClone(o: GitOptions): Builtin {
  return builtin({
    name: "git_clone",
    input: GitCloneInput,
    effect: "read_only",
    run: async (input, ctx, env) => {
      const c = checked(o, input, ctx);
      if (!c.ok) return c.error;
      const { dir, bundle } = c.value;
      const got = await bundled(o, input.repo, ctx);
      if (!got.ok) return got.error;
      const failed = await steps(env, ctx, got.value, bundle, [
        ["clone", "--quiet", bundle, dir],
        ["-C", dir, "remote", "set-url", "origin", remoteUrl(o, input.repo)],
        ...(input.ref === undefined
          ? []
          : [["-C", dir, "checkout", "--quiet", input.ref]]),
      ]);
      return (
        failed ??
        done(
          `cloned ${input.repo} into ${dir}${input.ref === undefined ? "" : ` at ${input.ref}`}`,
        )
      );
    },
  });
}

export function gitFetch(o: GitOptions): Builtin {
  return builtin({
    name: "git_fetch",
    input: GitFetchInput,
    effect: "read_only",
    run: async (input, ctx, env) => {
      const c = checked(o, input, ctx);
      if (!c.ok) return c.error;
      const { dir, bundle } = c.value;
      const got = await bundled(o, input.repo, ctx);
      if (!got.ok) return got.error;
      const refspec =
        input.ref === undefined
          ? "+refs/heads/*:refs/remotes/origin/*"
          : `+refs/heads/${input.ref}:refs/remotes/origin/${input.ref}`;
      const failed = await steps(env, ctx, got.value, bundle, [
        ["-C", dir, "fetch", "--quiet", "--tags", bundle, refspec],
      ]);
      return (
        failed ??
        done(
          `fetched ${input.ref ?? "every branch"} of ${input.repo} into ${dir} as origin/*`,
        )
      );
    },
  });
}

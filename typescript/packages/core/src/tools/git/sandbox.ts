import type { ToolRun } from "../../loop/types";
import { err, ok, type Result } from "../../result";
import { execute, toolRunOf } from "../../sandbox/exec";
import type { SandboxSession } from "../../sandbox/protocol";
import { type BuiltinEnv, done, failed, sessionOf } from "../builtin";

// The sandbox half of the git gateway: bundles go in and out through upload and download, and
// git runs in the sandbox with an empty environment, so no credential is ever there.

export const BUNDLES = "/tmp/.threads-git";
const TIMEOUT_MS = 300_000;

export type Sandboxed = {
  readonly session: SandboxSession;
  /** Runs git in the sandbox; a failed exit is an error run carrying its stderr. */
  readonly git: (...args: string[]) => Promise<Result<string, ToolRun>>;
};

export async function sandboxed(
  env: BuiltinEnv,
  processKey: string,
): Promise<Result<Sandboxed, ToolRun>> {
  const session = await sessionOf(env);
  if (!session.ok) return session;
  const git = async (...args: string[]): Promise<Result<string, ToolRun>> => {
    const ran = await execute(
      session.value,
      ["git", ...args],
      env.context,
      { env: {}, timeoutMs: TIMEOUT_MS, processKey },
      env.artifacts,
    );
    if (!ran.ok) return err(toolRunOf(ran));
    return ran.value.exit_code === 0
      ? ok(ran.value.stdout)
      : err(
          done(
            `git ${args[0] ?? ""} failed in the sandbox: ${ran.value.stderr}`,
            true,
          ),
        );
  };
  return ok({ session: session.value, git });
}

/** Puts a host bundle into the sandbox at `path`, making its directory first. */
export async function upload(
  s: Sandboxed,
  env: BuiltinEnv,
  path: string,
  bytes: Uint8Array,
): Promise<Result<void, ToolRun>> {
  const up = await s.session.upload(path, bytes, env.context);
  return up.ok ? ok(undefined) : err(failed(up.error));
}

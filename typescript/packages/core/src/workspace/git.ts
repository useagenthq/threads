import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { ConfigError } from "../agent/errors";
import { redactSecrets } from "../redact";
import {
  type ForgeAccess,
  type Ran,
  remoteUrl,
  runGit,
  safeRef,
  scratch,
} from "../tools/git/host";

// The host's git for workspace inputs: whether a directory is in a work tree, what that tree
// ignores (git decides, so neither language reimplements gitignore), its submodules, and a
// checkout of a forge repository. A local directory's git runs with the user's own environment,
// so the global excludes count (spec/schema/README.md, Workspace inputs).

const invalid = (message: string): ConfigError =>
  new ConfigError("invalid_config", redactSecrets(message));

/** The user's git in `dir`; code 127 when git can't be started. */
const local = (dir: string, ...args: string[]): Promise<Ran> =>
  runGit(["-C", dir, ...args], process.env, dir);

/** A `.git` in `dir` or any parent: a work tree as seen without git. */
function dotGitAbove(dir: string): boolean {
  for (let at = resolve(dir); ; at = dirname(at)) {
    if (existsSync(join(at, ".git"))) return true;
    if (dirname(at) === at) return false;
  }
}

/**
 * Whether `dir` is inside a git work tree. With no git on the host, a `.git` in it or any parent
 * is a work tree that can't be read: ConfigError, never a copy without its gitignore.
 */
export async function inWorkTree(dir: string, label: string): Promise<boolean> {
  const ran = await local(dir, "rev-parse", "--is-inside-work-tree");
  if (ran.code === 127) {
    if (dotGitAbove(dir))
      throw invalid(
        `${label} is inside a git repository and git isn't on this host's PATH; install git, or point localDir outside the repository`,
      );
    return false;
  }
  return ran.code === 0 && ran.stdout.trim() === "true";
}

function listed(ran: Ran, what: string, label: string): readonly string[] {
  if (ran.code !== 0)
    throw invalid(`${label}: git ${what} failed: ${ran.stderr}`);
  return ran.stdout.split("\0").filter((p) => p !== "");
}

/** The paths git ignores under `dir`, relative to it; an ignored directory without its `/`. */
export async function ignoredPaths(
  dir: string,
  label: string,
): Promise<ReadonlySet<string>> {
  const ran = await local(
    dir,
    "ls-files",
    "-z",
    "--others",
    "--ignored",
    "--exclude-standard",
    "--directory",
  );
  return new Set(
    listed(ran, "ls-files", label).map((p) =>
      p.endsWith("/") ? p.slice(0, -1) : p,
    ),
  );
}

/** The submodules (gitlinks, mode 160000) git tracks under `dir`, relative to it. */
export async function submodules(
  dir: string,
  label: string,
): Promise<ReadonlySet<string>> {
  const ran = await local(dir, "ls-files", "-z", "--stage");
  return new Set(
    listed(ran, "ls-files", label).flatMap((line) => {
      const tab = line.indexOf("\t");
      return line.startsWith("160000 ") && tab > 0 ? [line.slice(tab + 1)] : [];
    }),
  );
}

const REPO = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;

/**
 * Fetches `ref` of `repo` on the host (with the forge credential when there is one) into a
 * scratch checkout, runs `body` on it with the commit, then removes it. The checkout's own `.git`
 * holds no credential: the header rides in the environment.
 */
export async function withCheckout<T>(
  forge: ForgeAccess,
  repo: string,
  ref: string,
  body: (dir: string, commit: string) => Promise<T>,
): Promise<T> {
  const label = `workspace git ${repo}@${ref}`;
  if (!REPO.test(repo) || repo.includes(".."))
    throw invalid(`${label}: repo must be owner/name`);
  if (!safeRef(ref))
    throw invalid(`${label}: ref must be a plain branch, tag or commit name`);
  return scratch(forge, async (git, dir) => {
    const step = async (...args: string[]): Promise<string> => {
      const ran = await git(...args);
      if (ran.code !== 0)
        throw invalid(`${label}: git ${args.join(" ")} failed: ${ran.stderr}`);
      return ran.stdout;
    };
    await step("init", "--quiet", "w");
    await step(
      "-C",
      "w",
      "fetch",
      "--quiet",
      "--depth",
      "1",
      remoteUrl(forge, repo),
      ref,
    );
    await step("-C", "w", "checkout", "--quiet", "--detach", "FETCH_HEAD");
    const commit = (await step("-C", "w", "rev-parse", "HEAD")).trim();
    return body(join(dir, "w"), commit);
  });
}

import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, posix } from "node:path";
import type { Secret } from "../../agent/secret";

// The host half of the git gateway: git runs on the host with the forge
// credential in its environment (an http.extraHeader through GIT_CONFIG_*, never in argv or a
// URL), in a throwaway directory, with no user or system config. Nothing here reaches the
// sandbox except git bundles, which carry objects and refs only (invariant 4).

export type GitOptions = {
  readonly credential: Secret;
  /** Where owner/name lives: <forgeUrl>/<owner>/<name>.git. Default https://github.com. */
  readonly forgeUrl?: string;
  /** The forge REST API for pull requests. Default https://api.github.com. */
  readonly apiUrl?: string;
  /** The host's HTTP client for the forge API; injectable for tests. */
  readonly fetch?: (url: string, init: RequestInit) => Promise<Response>;
};

export type Ran = {
  readonly code: number;
  readonly stdout: string;
  readonly stderr: string;
};

export const remoteUrl = (o: ForgeAccess, repo: string): string =>
  `${(o.forgeUrl ?? "https://github.com").replace(/\/$/, "")}/${repo}.git`;

/** Where a host git run fetches from, and with what: the forge credential when there is one. */
export type ForgeAccess = Pick<GitOptions, "forgeUrl"> & {
  readonly credential?: Secret;
};

/** The environment for one host git run: the credential as a header, nothing inherited. */
function gitEnv(o: ForgeAccess, home: string): Record<string, string> {
  const config: (readonly [string, string])[] = [
    // A bundle from the sandbox is untrusted input: every object is checked.
    ["transfer.fsckObjects", "true"],
  ];
  if (o.credential !== undefined) {
    const token = `x-access-token:${o.credential.reveal()}`;
    const basic = Buffer.from(token).toString("base64");
    config.push(["http.extraHeader", `Authorization: Basic ${basic}`]);
  }
  return {
    PATH: process.env["PATH"] ?? "/usr/bin:/bin",
    HOME: home,
    GIT_TERMINAL_PROMPT: "0",
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: "/dev/null",
    GIT_CONFIG_COUNT: String(config.length),
    ...Object.fromEntries(
      config.flatMap(([key, value], i) => [
        [`GIT_CONFIG_KEY_${i}`, key],
        [`GIT_CONFIG_VALUE_${i}`, value],
      ]),
    ),
  };
}

/** One git run on the host; a git that can't be started is code 127. */
export function runGit(
  args: readonly string[],
  env: Readonly<Record<string, string | undefined>>,
  cwd: string,
): Promise<Ran> {
  const { promise, resolve } = Promise.withResolvers<Ran>();
  const child = spawn("git", args, {
    cwd,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const out: Buffer[] = [];
  const err: Buffer[] = [];
  child.stdout.on("data", (b: Buffer) => out.push(b));
  child.stderr.on("data", (b: Buffer) => err.push(b));
  child.on("error", (e) =>
    resolve({ code: 127, stdout: "", stderr: String(e) }),
  );
  child.on("close", (code) =>
    resolve({
      code: code ?? 1,
      stdout: Buffer.concat(out).toString("utf8"),
      stderr: Buffer.concat(err).toString("utf8"),
    }),
  );
  return promise;
}

/** A scratch directory on the host for one operation, removed afterwards. */
export async function scratch<T>(
  o: ForgeAccess,
  body: (git: (...args: string[]) => Promise<Ran>, dir: string) => Promise<T>,
): Promise<T> {
  const dir = await mkdtemp(join(tmpdir(), "threads-git-"));
  try {
    const env = gitEnv(o, dir);
    return await body((...args) => runGit(args, env, dir), dir);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

/** A branch, tag or commit name safe to pass as one argv element. */
export const safeRef = (ref: string): boolean =>
  /^[A-Za-z0-9._/-]+$/.test(ref) &&
  !ref.startsWith("-") &&
  !ref.includes("..") &&
  !ref.endsWith(".lock") &&
  !ref.endsWith("/");

/** The clone's directory under /workspace, or undefined when it would leave it. */
export function cloneDir(
  repo: string,
  path: string | undefined,
): string | undefined {
  const name = repo.split("/")[1] ?? repo;
  const dir = posix.resolve("/workspace", path ?? name);
  return dir.startsWith("/workspace/") ? dir : undefined;
}

import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

// One host directory per test, with the files a real repository has around a workspace input.

export type Files = Readonly<Record<string, string>>;

/** Makes a temporary directory holding `files` (path → contents); 0o755 when the path ends `*`. */
export function dirOf(files: Files): string {
  const root = mkdtempSync(join(tmpdir(), "threads-ws-"));
  for (const [raw, body] of Object.entries(files)) {
    const exec = raw.endsWith("*");
    const path = join(root, exec ? raw.slice(0, -1) : raw);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, body, { mode: exec ? 0o755 : 0o644 });
  }
  return root;
}

export function link(root: string, at: string, target: string): void {
  mkdirSync(dirname(join(root, at)), { recursive: true });
  symlinkSync(target, join(root, at));
}

/** Runs git in `dir`; the test is skipped by the caller when git isn't on this host. */
export function git(dir: string, ...args: string[]): void {
  const ran = spawnSync("git", args, { cwd: dir, encoding: "utf8" });
  if (ran.status !== 0)
    throw new Error(`git ${args.join(" ")}: ${ran.stderr ?? ran.error}`);
}

/** A repository with one commit, so `git ls-files` answers. */
export function repoOf(files: Files): string {
  const root = dirOf(files);
  git(root, "init", "--quiet");
  git(root, "config", "user.email", "t@example.com");
  git(root, "config", "user.name", "t");
  git(root, "config", "commit.gpgsign", "false");
  return root;
}

export const hasGit: boolean =
  spawnSync("git", ["--version"], { encoding: "utf8" }).status === 0;

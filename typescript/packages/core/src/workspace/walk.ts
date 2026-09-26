import type { Stats } from "node:fs";
import { lstat, readdir, readlink } from "node:fs/promises";
import { join } from "node:path";
import { ConfigError } from "../agent/errors";
import { assertNever } from "../assert-never";
import { inRoot } from "../sandbox/tree/paths";
import { denied, excluded } from "./exclude";
import { ignoredPaths, inWorkTree, submodules } from "./git";

// Reads one host directory for a workspace (spec/schema/README.md, Workspace inputs): which paths
// it copies, which it skips (.git, gitignored, deny-listed), and which `include` re-admits.

/** A path the tree takes: a file still on the host, a directory or a symlink. */
export type Found =
  | {
      readonly kind: "file";
      readonly path: string;
      readonly mode: number;
      readonly size: number;
      readonly host: string;
    }
  | { readonly kind: "dir"; readonly path: string; readonly mode: number }
  | {
      readonly kind: "symlink";
      readonly path: string;
      readonly target: string;
    };

/** The limits' running totals, shared by every input (counted after exclusions). */
export type Counted = { files: number; bytes: number };

const MAX_FILES = 10_000;
const MAX_BYTES = 256 * 1024 * 1024;

/**
 * How a directory's children are read: `normal`; `partial`, below a skipped directory, where
 * only paths on the way to an include get through; or `included` from segment `start`, inside an
 * included directory, where only `.git` and deny-listed names below it are skipped.
 */
type State =
  | { readonly kind: "normal" }
  | { readonly kind: "partial" }
  | { readonly kind: "included"; readonly start: number };

type Walk = {
  readonly root: string;
  readonly label: string;
  readonly include: readonly string[];
  readonly ignored: ReadonlySet<string>;
  readonly gitlinks: ReadonlySet<string>;
  readonly used: Set<string>;
  readonly counted: Counted;
  readonly found: Found[];
  readonly skipped: string[];
};

const invalid = (message: string): ConfigError =>
  new ConfigError("invalid_config", message);

const NORMAL: State = { kind: "normal" };
const PARTIAL: State = { kind: "partial" };

const below = (w: Walk, path: string): boolean =>
  w.include.some((i) => i.startsWith(`${path}/`));

/**
 * An `include` entry: its subtree comes in, minus `.git` and deny-listed names below it. The
 * entry counts as used only if something actually skipped this path.
 */
function reAdmit(w: Walk, path: string, state: State): State {
  const skipped =
    state.kind === "partial" ||
    (state.kind === "normal" && (lastDenied(path) || w.ignored.has(path)));
  if (skipped) w.used.add(path);
  return { kind: "included", start: path.split("/").length };
}

/** The state a path is read in, or undefined when it is skipped. */
function decide(w: Walk, path: string, state: State): State | undefined {
  if (w.include.includes(path)) return reAdmit(w, path, state);
  switch (state.kind) {
    case "included":
      return excluded(path, state.start) ? undefined : state;
    case "partial":
      return below(w, path) ? PARTIAL : undefined;
    case "normal":
      if (!lastDenied(path) && !w.ignored.has(path)) return NORMAL;
      return below(w, path) ? PARTIAL : undefined;
    default:
      return assertNever(state);
  }
}

function lastDenied(path: string): boolean {
  const parts = path.split("/");
  return denied(parts, parts.length - 1);
}

/** Throws once the inputs pass a limit. */
export function checkLimits(counted: Counted): void {
  if (counted.files > MAX_FILES)
    throw invalid(
      `workspace: more than ${MAX_FILES} files after exclusions; point localDir at a smaller directory, add a .gitignore, or use the git input`,
    );
  if (counted.bytes > MAX_BYTES)
    throw invalid(
      `workspace: more than 256 MiB of files after exclusions; point localDir at a smaller directory, add a .gitignore, or use the git input`,
    );
}

async function exists(host: string): Promise<boolean> {
  try {
    await lstat(host);
    return true;
  } catch {
    return false;
  }
}

async function nested(w: Walk, path: string, state: State): Promise<void> {
  if (state.kind !== "normal") return;
  if (w.gitlinks.has(path) || (await exists(join(w.root, path, ".git"))))
    throw invalid(
      `${w.label}: ${path} is a git submodule or repository; submodules aren't copied; use the git input or a vendored copy`,
    );
}

async function admit(
  w: Walk,
  path: string,
  st: Stats,
  state: State,
): Promise<void> {
  if (st.isSymbolicLink()) {
    const target = await readlink(join(w.root, path));
    if (!inRoot(path, target))
      throw invalid(
        `${w.label}: symlink ${path} points outside it (${target})`,
      );
    w.found.push({ kind: "symlink", path, target });
  } else if (st.isDirectory()) {
    await nested(w, path, state);
    w.found.push({ kind: "dir", path, mode: 0o755 });
    await walkDir(w, path, state);
  } else if (st.isFile()) {
    const mode = (st.mode & 0o111) === 0 ? 0o644 : 0o755;
    w.found.push({
      kind: "file",
      path,
      mode,
      size: st.size,
      host: join(w.root, path),
    });
    w.counted.files += 1;
    w.counted.bytes += st.size;
    checkLimits(w.counted);
  } else
    throw invalid(
      `${w.label}: ${path} is not a regular file, directory or symlink`,
    );
}

async function walkDir(w: Walk, dir: string, state: State): Promise<void> {
  const names = (await readdir(join(w.root, dir))).toSorted();
  for (const name of names) {
    const path = dir === "" ? name : `${dir}/${name}`;
    const next = name === ".git" ? undefined : decide(w, path, state);
    if (next === undefined) w.skipped.push(path);
    else await admit(w, path, await lstat(join(w.root, path)), next);
  }
}

/** What reading one directory gives: its paths, and the top-most ones it skipped, sorted. */
export type Read = {
  readonly found: readonly Found[];
  readonly skipped: readonly string[];
};

/**
 * Reads `root` (an existing host directory) as a workspace source. `used` collects the include
 * entries that re-admitted a skipped path; `counted` carries the limits across inputs.
 */
export async function readDir(
  root: string,
  label: string,
  include: readonly string[],
  used: Set<string>,
  counted: Counted,
): Promise<Read> {
  const git = await inWorkTree(root, label);
  const w: Walk = {
    root,
    label,
    include,
    ignored: git ? await ignoredPaths(root, label) : new Set(),
    gitlinks: git ? await submodules(root, label) : new Set(),
    used,
    counted,
    found: [],
    skipped: [],
  };
  await walkDir(w, "", NORMAL);
  return { found: w.found, skipped: w.skipped.toSorted() };
}

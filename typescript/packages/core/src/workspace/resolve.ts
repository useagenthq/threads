import { lstat, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { ConfigError } from "../agent/errors";
import { sha256Hex } from "../hash";
import type { WorkspacePin, WorkspaceSource } from "../log";
import { containsSecret } from "../redact/bytes";
import { isNormal, pathSet } from "../sandbox/tree/paths";
import {
  encodeTree,
  type Tree,
  type TreeEntry,
  treeManifestHash,
} from "../sandbox/tree/tree";
import type { ForgeAccess } from "../tools/git/host";
import { withCheckout } from "./git";
import {
  type Counted,
  checkLimits,
  type Found,
  type Read,
  readDir,
} from "./walk";

// Resolves agent({workspace}) into one tree on the host (spec/schema/README.md, Workspace
// inputs): every input read, excluded, checked for registered secrets and collisions, each file
// and the tree kept as artifacts, and the pin that names them. Every error is ConfigError.

/** spec/api.json Workspace. */
export type Workspace = {
  /** Tree path → contents (text as UTF-8), mode 0o644, taken as given. */
  readonly files?: Readonly<Record<string, string | Uint8Array>>;
  /** A host directory, read on the host and pinned exactly as written. */
  readonly localDir?: string;
  /** A repository on the forge of the agent's git option, fetched on the host. */
  readonly git?: { readonly repo: string; readonly ref: string };
  /** Skipped tree paths to re-admit, one by one; a directory re-admits its subtree. */
  readonly include?: readonly string[];
};

/** Keeps one artifact's bytes: the store's put, or nothing for check(). */
export type Keep = (bytes: Uint8Array) => Promise<unknown>;

/** A tree path and where its bytes come from, before they are read. */
type Pending =
  | Exclude<TreeEntry, { readonly kind: "file" }>
  | {
      readonly kind: "file";
      readonly path: string;
      readonly mode: number;
      readonly load: () => Promise<Uint8Array>;
    };

const invalid = (message: string): ConfigError =>
  new ConfigError("invalid_config", message);

const encoder = new TextEncoder();

function checkIncludes(include: readonly string[]): void {
  for (const path of include)
    if (!isNormal(path) || path.split("/").includes(".git"))
      throw invalid(
        `workspace include ${JSON.stringify(path)}: a normal tree path outside .git`,
      );
}

function fromFiles(
  files: NonNullable<Workspace["files"]>,
  counted: Counted,
): readonly Pending[] {
  return Object.entries(files).map(([path, value]) => {
    if (!isNormal(path))
      throw invalid(
        `workspace files: ${JSON.stringify(path)} is not a normal tree path`,
      );
    const bytes =
      typeof value === "string" ? encoder.encode(value) : value.slice();
    counted.files += 1;
    counted.bytes += bytes.length;
    return { kind: "file", path, mode: 0o644, load: async () => bytes };
  });
}

function pending(read: Read): readonly Pending[] {
  return read.found.map((f: Found): Pending => {
    if (f.kind !== "file") return f;
    return {
      kind: "file",
      path: f.path,
      mode: f.mode,
      load: async () => new Uint8Array(await readFile(f.host)),
    };
  });
}

async function fromLocal(
  path: string,
  include: readonly string[],
  used: Set<string>,
  counted: Counted,
): Promise<{
  readonly source: WorkspaceSource;
  readonly found: readonly Pending[];
}> {
  const label = `workspace localDir ${path}`;
  const root = resolve(path);
  let isDir = false;
  try {
    isDir = (await lstat(root)).isDirectory();
  } catch {
    // Named below.
  }
  if (!isDir) throw invalid(`${label} is not a readable directory`);
  const read = await readDir(root, label, include, used, counted);
  return {
    source: { kind: "local_dir", path, skipped: [...read.skipped] },
    found: pending(read),
  };
}

async function fromGit(
  git: NonNullable<Workspace["git"]>,
  forge: ForgeAccess,
  include: readonly string[],
  used: Set<string>,
  counted: Counted,
): Promise<{
  readonly source: WorkspaceSource;
  readonly found: readonly Pending[];
}> {
  const label = `workspace git ${git.repo}@${git.ref}`;
  return withCheckout(forge, git.repo, git.ref, async (dir, commit) => {
    const read = await readDir(dir, label, include, used, counted);
    // The checkout is removed when this returns, so its files are read now.
    // ponytail: they are held in memory (bounded by the 256 MiB limit); spill to disk if that bites.
    const found: Pending[] = [];
    for (const entry of pending(read)) {
      if (entry.kind !== "file") {
        found.push(entry);
        continue;
      }
      const bytes = await entry.load();
      found.push({ ...entry, load: async () => bytes });
    }
    return {
      source: {
        kind: "git",
        repo: git.repo,
        ref: git.ref,
        commit,
        skipped: [...read.skipped],
      },
      found,
    };
  });
}

/** The entries sorted, each path once and never under a file or symlink. */
function merged(all: readonly Pending[]): readonly Pending[] {
  const sorted = all.toSorted((a, b) =>
    a.path < b.path ? -1 : a.path > b.path ? 1 : 0,
  );
  const paths = pathSet();
  for (const e of sorted) {
    const clash = paths.add(e.path, e.kind);
    if (clash !== undefined)
      throw invalid(
        `workspace: two inputs give ${e.path}${clash === "duplicate" ? "" : ", or a file where another has a directory"}`,
      );
  }
  return sorted;
}

async function loaded(entries: readonly Pending[], keep: Keep): Promise<Tree> {
  const out: TreeEntry[] = [];
  for (const e of entries) {
    if (e.kind !== "file") {
      out.push(e);
      continue;
    }
    const data = await e.load();
    if (containsSecret(data))
      throw invalid(
        `workspace file ${e.path} contains a secret value; remove it or exclude the file`,
      );
    await keep(data);
    out.push({
      kind: "file",
      path: e.path,
      mode: e.mode,
      size: data.length,
      sha256: sha256Hex(data),
    });
  }
  return { tree_version: 1, entries: out };
}

/** The pin the thread carries, and the tree each new sandbox is given. */
export type Resolved = {
  readonly pin: WorkspacePin;
  readonly tree: Tree;
};

/**
 * Resolves the workspace into its pin, keeping each file and the tree with `keep` first. Runs
 * after setup, so every secret the agent resolves is registered. Throws ConfigError.
 */
export async function resolveWorkspace(
  ws: Workspace,
  forge: ForgeAccess,
  keep: Keep,
): Promise<Resolved> {
  const include = ws.include ?? [];
  checkIncludes(include);
  const used = new Set<string>();
  const counted: Counted = { files: 0, bytes: 0 };
  const sources: WorkspaceSource[] = [];
  const all: Pending[] = [];
  if (ws.files !== undefined) {
    sources.push({ kind: "files" });
    all.push(...fromFiles(ws.files, counted));
    checkLimits(counted);
  }
  for (const got of [
    ws.localDir === undefined
      ? undefined
      : await fromLocal(ws.localDir, include, used, counted),
    ws.git === undefined
      ? undefined
      : await fromGit(ws.git, forge, include, used, counted),
  ]) {
    if (got === undefined) continue;
    sources.push(got.source);
    all.push(...got.found);
  }
  const unused = include.find((i) => !used.has(i));
  if (unused !== undefined)
    throw invalid(
      `workspace include ${unused} re-admits nothing: no input skipped it`,
    );
  const tree = await loaded(merged(all), keep);
  const bytes = encodeTree(tree);
  await keep(bytes);
  return {
    pin: {
      tree: { sha256: sha256Hex(bytes), bytes: bytes.length },
      manifest_hash: treeManifestHash(tree),
      sources,
    },
    tree,
  };
}

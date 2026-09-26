import { sha256Hex } from "../../hash";
import { err, ok, type Result } from "../../result";
import { memoryArtifacts } from "../../store/artifacts";
import { logError } from "../../verify/error";
import type { ExecOutput, Failure } from "../protocol";
import { buildTar } from "../tree/build";
import { readTar } from "../tree/tar";
import type { Tree, TreeEntry } from "../tree/tree";
import { type Found, failed, mkdirIn, readIn, symlinkIn, walkTree, writeIn } from "./walk";

// The Trees capability directly on the sandbox's host directory: no archive tool runs, and no
// symlink is followed (walk.ts). Modes are the directory's own; the builder masks setuid,
// setgid and sticky bits out of the archive, as every other kit does.
//
// ponytail: both directions hold one archive in memory, as placeTree already does; the reader's
// 1 GiB archive cap bounds it. Stream them when a reader wants a pull source.

type Unavailable = Failure<"unavailable">;

const ROOT = { uid: 0, gid: 0 };

async function* one(bytes: Uint8Array): AsyncIterable<Uint8Array> {
  yield bytes;
}

async function* none(): AsyncIterable<Uint8Array> {
  // The export writes nothing to stderr: there is no command to fail.
}

/** The walked directory as a tree, each file hashed where it lies. */
function treeOf(
  dir: string,
  found: readonly Found[],
): Result<{ tree: Tree; at: ReadonlyMap<string, readonly string[]> }, Unavailable> {
  const entries: TreeEntry[] = [];
  const at = new Map<string, readonly string[]>();
  for (const e of found) {
    if (e.kind === "dir") {
      entries.push({ path: e.path, kind: "dir", mode: e.mode });
      continue;
    }
    if (e.kind === "symlink") {
      entries.push({ path: e.path, kind: "symlink", target: e.target });
      continue;
    }
    const parts = e.path.split("/");
    const bytes = readIn(dir, parts);
    if (!bytes.ok)
      return err({ code: "unavailable", message: bytes.error.message });
    const sha256 = sha256Hex(bytes.value);
    at.set(sha256, parts);
    entries.push({
      path: e.path,
      kind: "file",
      mode: e.mode,
      size: bytes.value.length,
      sha256,
    });
  }
  return ok({ tree: { tree_version: 1, entries }, at });
}

/** The directory as one tar archive, exactly as a remote sandbox's `tar -cf -` would answer. */
export function exportDir(dir: string): Promise<Result<ExecOutput, Unavailable>> {
  return (async () => {
    const found = walkTree(dir);
    if (!found.ok)
      return err({ code: "unavailable" as const, message: found.error.message });
    const made = treeOf(dir, found.value);
    if (!made.ok) return made;
    const chunks: Uint8Array[] = [];
    const built = await buildTar(
      made.value.tree,
      {
        get: async (sha256) => {
          const parts = made.value.at.get(sha256);
          const bytes = parts === undefined ? undefined : readIn(dir, parts);
          if (bytes === undefined || !bytes.ok)
            return err(logError("artifact_corrupt", `${sha256} left ${dir}`));
          return ok(bytes.value);
        },
      },
      ROOT,
      (chunk) => chunks.push(chunk),
    );
    if (!built.ok)
      return err({ code: "unavailable" as const, message: built.error.message });
    let size = 0;
    for (const c of chunks) size += c.length;
    const whole = new Uint8Array(size);
    let offset = 0;
    for (const c of chunks) {
      whole.set(c, offset);
      offset += c.length;
    }
    return ok({
      exit_code: Promise.resolve(0),
      stdout: one(whole),
      stderr: none(),
    });
  })();
}

/** Extracts a host-built archive into the directory, keeping modes and symlinks. */
export async function importDir(
  dir: string,
  tar: AsyncIterable<Uint8Array>,
): Promise<Result<void, Unavailable>> {
  const artifacts = memoryArtifacts();
  const tree = await readTar(tar, artifacts.sink);
  if (!tree.ok)
    return err({ code: "unavailable", message: tree.error.message });
  for (const e of tree.value.entries) {
    const parts = e.path.split("/");
    if (e.kind === "dir") {
      const made = mkdirIn(dir, parts, e.mode & 0o777);
      if (!made.ok) return err({ code: "unavailable", message: made.error.message });
      continue;
    }
    if (e.kind === "symlink") {
      const made = symlinkIn(dir, parts, e.target);
      if (!made.ok) return err({ code: "unavailable", message: made.error.message });
      continue;
    }
    const bytes = await artifacts.get(e.sha256);
    if (!bytes.ok) return err({ code: "unavailable", message: bytes.error.message });
    const written = writeIn(dir, parts, bytes.value, e.mode & 0o777);
    if (!written.ok)
      return err({ code: "unavailable", message: written.error.message });
  }
  return ok(undefined);
}

export { failed };

import { err, ok, type Result } from "../result";
import { memoryArtifacts } from "../store/artifacts";
import type { Trees } from "./protocol";
import { buildTar } from "./tree/build";
import { type ArchiveInvalid, readTar } from "./tree/tar";
import type { TreeEntry } from "./tree/tree";

// The Trees capability for in-memory sandboxes (the fake, and the tests' emulated machines): a
// tree held in memory, archived with the host builder and extracted with the host reader.

/** One entry of an in-memory tree, by path relative to /workspace. */
export type MemoryEntry =
  | {
      readonly path: string;
      readonly mode: number;
      readonly bytes: Uint8Array;
    }
  | { readonly path: string; readonly target: string };

/** The tar archive of an in-memory tree. */
export async function archiveOf(
  entries: readonly MemoryEntry[],
): Promise<Uint8Array> {
  const artifacts = memoryArtifacts();
  const tree: TreeEntry[] = [];
  for (const e of entries)
    tree.push(
      "target" in e
        ? { path: e.path, kind: "symlink", target: e.target }
        : {
            path: e.path,
            kind: "file",
            mode: e.mode,
            size: e.bytes.length,
            sha256: await artifacts.put(e.bytes),
          },
    );
  const parts: Uint8Array[] = [];
  // Owner 0: an in-memory tree has none, and a reader ignores it.
  const built = await buildTar(
    {
      tree_version: 1,
      entries: tree.toSorted((a, b) =>
        a.path < b.path ? -1 : a.path > b.path ? 1 : 0,
      ),
    },
    artifacts,
    { uid: 0, gid: 0 },
    (chunk) => parts.push(chunk),
  );
  if (!built.ok)
    throw new Error(`an in-memory tree is valid: ${built.error.message}`);
  return new Uint8Array(parts.flatMap((p) => [...p]));
}

/** An archive's files (modes as it gives them) and symlinks; directories need no entry here. */
export async function extract(
  tar: AsyncIterable<Uint8Array>,
): Promise<Result<readonly MemoryEntry[], ArchiveInvalid>> {
  const artifacts = memoryArtifacts();
  const tree = await readTar(tar, artifacts.sink);
  if (!tree.ok) return tree;
  const out: MemoryEntry[] = [];
  for (const e of tree.value.entries) {
    if (e.kind === "symlink") out.push({ path: e.path, target: e.target });
    if (e.kind !== "file") continue;
    const bytes = await artifacts.get(e.sha256);
    if (!bytes.ok) throw new Error(`a file just read is stored: ${e.path}`);
    out.push({ path: e.path, mode: e.mode, bytes: bytes.value });
  }
  return ok(out);
}

const WORKSPACE = "/workspace/";

async function* chunks(bytes: Uint8Array): AsyncIterable<Uint8Array> {
  for (let at = 0; at < bytes.length; at += 4096)
    yield bytes.subarray(at, at + 4096);
}

/**
 * The fake's Trees over one sandbox's files (absolute paths). Modes and symlinks live beside
 * them; a file written any other way reads as 0o644. The fake has no way to set a setuid bit,
 * so exporting through the (masking) builder reports every mode it holds.
 */
export function fakeTrees(files: Map<string, Uint8Array>): Trees {
  const modes = new Map<string, number>();
  const links = new Map<string, string>();
  return {
    exportTree: async (context) => {
      const fenced = await context.fence();
      if (!fenced.ok) return fenced;
      const entries: MemoryEntry[] = [
        ...files
          .entries()
          .filter(([at]) => at.startsWith(WORKSPACE))
          .map(([at, bytes]) => ({
            path: at.slice(WORKSPACE.length),
            mode: modes.get(at) ?? 0o644,
            bytes,
          })),
        ...links.entries().map(([at, target]) => ({
          path: at.slice(WORKSPACE.length),
          target,
        })),
      ];
      return ok({
        exit_code: Promise.resolve(0),
        stdout: chunks(await archiveOf(entries)),
        stderr: chunks(new Uint8Array()),
      });
    },
    importTree: async (tar, context) => {
      const fenced = await context.fence();
      if (!fenced.ok) return fenced;
      const got = await extract(tar);
      if (!got.ok)
        return err({ code: "unavailable", message: got.error.message });
      for (const e of got.value) {
        const at = `${WORKSPACE}${e.path}`;
        if ("target" in e) links.set(at, e.target);
        else {
          files.set(at, e.bytes);
          modes.set(at, e.mode);
        }
      }
      return ok(undefined);
    },
  };
}

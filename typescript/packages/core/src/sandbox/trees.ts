import { createHash } from "node:crypto";
import { err, ok, type Result } from "../result";
import type { ArtifactSink, ArtifactStore } from "../store/artifacts";
import type { LogError } from "../verify/error";
import type { Failure, SandboxContext, Stale, Trees } from "./protocol";
import { buildTar, type Owner } from "./tree/build";
import { type ArchiveInvalid, readTar } from "./tree/tar";
import { masked, type Tree, treeManifestHash } from "./tree/tree";

// Core's side of the Trees capability (spec/api.json SandboxSession.exportTree, importTree):
// every export is read through the strict tree reader, and a placed tree is proven by
// re-exporting it. Tree bytes go straight to the artifact store's sink, never through the
// redacting Spill: redaction would change the bytes and their hashes.

export type ReadFailure = ArchiveInvalid | Failure<"unavailable"> | Stale;

/**
 * Why a tree couldn't be placed. Core-internal codes: workspace inputs report tree_mismatch as
 * workspace_mismatch, and a host-snapshot restore as snapshot_manifest_mismatch.
 */
export type Misplaced = {
  readonly code: "workspace_not_empty" | "tree_mismatch";
  readonly message: string;
};

export type PlaceFailure = ReadFailure | LogError | Misplaced;

/** --no-same-owner drops the archive's owner, so the builder's is only a placeholder. */
const ROOT: Owner = { uid: 0, gid: 0 };
const STDERR_KEPT = 4096;

/** A sink that only hashes: measures a tree's files without storing them. */
export function hashOnly(): ArtifactSink {
  const hash = createHash("sha256");
  let bytes = 0;
  return {
    write: (chunk) => {
      hash.update(chunk);
      bytes += chunk.length;
    },
    finish: () => Promise.resolve({ sha256: hash.digest("hex"), bytes }),
    abort: () => undefined,
  };
}

/** The head of a stream as text, draining the rest so its producer never stalls. */
async function head(stream: AsyncIterable<Uint8Array>): Promise<string> {
  const kept: Uint8Array[] = [];
  let size = 0;
  try {
    for await (const chunk of stream) {
      if (size < STDERR_KEPT) kept.push(chunk.subarray(0, STDERR_KEPT - size));
      size += chunk.length;
    }
  } catch {
    // The exit code carries the failure.
  }
  return new TextDecoder().decode(new Uint8Array(kept.flatMap((c) => [...c])));
}

async function settled(
  exit: Promise<number>,
): Promise<Result<number, Failure<"unavailable">>> {
  try {
    return ok(await exit);
  } catch (error) {
    return err({ code: "unavailable", message: String(error) });
  }
}

/**
 * Reads the session's export of /workspace into a tree, each file into a sink from `open`
 * (`hashOnly` to measure, `ArtifactStore.sink` to keep the files).
 */
export async function readTree(
  session: Pick<Trees, "exportTree">,
  context: SandboxContext,
  open: () => ArtifactSink,
): Promise<Result<Tree, ReadFailure>> {
  const exported = await session.exportTree(context);
  if (!exported.ok) return exported;
  const output = exported.value;
  const exit = settled(output.exit_code);
  const stderr = head(output.stderr);
  let tree: Awaited<ReturnType<typeof readTar>>;
  try {
    tree = await readTar(output.stdout, open);
  } catch (error) {
    return err({ code: "unavailable", message: String(error) });
  }
  // A refused archive is the answer: a hostile exporter may never exit.
  if (!tree.ok) return tree;
  const code = await exit;
  if (!code.ok) return code;
  if (code.value !== 0)
    return err({
      code: "unavailable",
      message: `the export of /workspace exited ${code.value}: ${await stderr}`,
    });
  return tree;
}

/** The manifest hash of the sandbox's /workspace, measured through its export. */
export async function treeHash(
  session: Pick<Trees, "exportTree">,
  context: SandboxContext,
): Promise<Result<string, ReadFailure>> {
  const tree = await readTree(session, context, hashOnly);
  return tree.ok ? ok(treeManifestHash(tree.value)) : tree;
}

const symlinks = (tree: Tree): string =>
  JSON.stringify(tree.entries.filter((e) => e.kind === "symlink"));

async function* each(chunks: readonly Uint8Array[]): AsyncIterable<Uint8Array> {
  yield* chunks;
}

/**
 * Places `tree` into the session's empty /workspace, then re-exports it and checks its files
 * (manifest hash) and symlinks. Modes are compared masked: setuid, setgid and sticky bits never
 * survive an import.
 */
export async function placeTree(
  session: Trees,
  tree: Tree,
  artifacts: Pick<ArtifactStore, "get">,
  context: SandboxContext,
): Promise<Result<void, PlaceFailure>> {
  const before = await readTree(session, context, hashOnly);
  if (!before.ok) return before;
  const first = before.value.entries[0];
  if (first !== undefined)
    return err({
      code: "workspace_not_empty",
      message: `the sandbox's /workspace isn't empty (${first.path}); workspace inputs and restores need it empty`,
    });
  // ponytail: the archive is held whole (kits upload one file); stream it when an adapter can.
  const chunks: Uint8Array[] = [];
  const built = await buildTar(tree, artifacts, ROOT, (chunk) =>
    chunks.push(chunk),
  );
  if (!built.ok) return built;
  const imported = await session.importTree(each(chunks), context);
  if (!imported.ok) return imported;
  const after = await readTree(session, context, hashOnly);
  if (!after.ok) return after;
  const want = masked(tree);
  if (
    treeManifestHash(after.value) === treeManifestHash(want) &&
    symlinks(after.value) === symlinks(want)
  )
    return ok(undefined);
  return err({
    code: "tree_mismatch",
    message: `/workspace doesn't hold the placed tree (manifest ${treeManifestHash(want)})`,
  });
}

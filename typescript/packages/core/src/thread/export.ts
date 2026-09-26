import {
  closeSync,
  fsyncSync,
  mkdirSync,
  openSync,
  renameSync,
  rmSync,
  writeSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { sha256Hex } from "../hash";
import type { BranchId } from "../log";
import { err, ok, type Result } from "../result";
import type { ArtifactStore } from "../store";
import { fsyncDir } from "../store/artifacts";
import type { LogStore } from "../store/store";
import { verifyExport } from "../verify";
import { type LogError, logError } from "../verify/error";
import {
  ARTIFACTS_DIR,
  type Bundle,
  encodeBundle,
  ioError,
  LOG_FILE,
  MANIFEST,
} from "./bundle";
import { refsIn } from "./save-case";

// thread.export (spec/api.json): a portable bundle directory, log.jsonl plus every artifact the
// chain names. The layout and its publication rule are in spec/schema/README.md, "Portable
// bundles": export claims `path` with an exclusive mkdir, writes and fsyncs the files, and
// publishes by renaming bundle.json into place last, so a crash leaves a directory that import
// refuses rather than a bundle that looks whole.

/** spec/api.json ExportedBundle: where the bundle is, what it holds. */
export type ExportedBundle = {
  readonly path: string;
  readonly branch_id: BranchId;
  readonly artifacts: number;
};

export async function exportBundle(
  log: LogStore,
  artifacts: ArtifactStore,
  branchId: BranchId,
  path: string,
): Promise<Result<ExportedBundle, LogError>> {
  const collected = await collect(log, artifacts, branchId);
  if (!collected.ok) return collected;
  const claimed = claim(path);
  if (!claimed.ok) return claimed;
  try {
    return ok(publish(path, branchId, collected.value));
  } catch (error) {
    discard(path);
    return err(ioError(error, path));
  }
}

/** What a failed export wrote, best effort: a directory with no bundle.json is refused anyway. */
function discard(path: string): void {
  try {
    rmSync(path, { recursive: true, force: true });
  } catch {
    // The caller already has the failure that matters; a stuck directory is not a second one.
  }
}

// ponytail: every artifact is held in memory while the bundle is written; stream them through
// the artifact sink if bundles of very large spilled outputs become a problem.
type Contents = {
  readonly log: Uint8Array;
  readonly artifacts: ReadonlyMap<string, Uint8Array>;
};

/** The chain's bytes and every artifact it names, all read and verified before anything is written. */
async function collect(
  log: LogStore,
  artifacts: ArtifactStore,
  branchId: BranchId,
): Promise<Result<Contents, LogError>> {
  const bytes = await log.exportBranch(branchId);
  if (!bytes.ok) return bytes;
  const verified = verifyExport(bytes.value);
  if (!verified.ok) return verified;
  // Walked over the stored lines, not the parsed events, so a ref inside an event this version
  // doesn't know is still bundled — the same set Python's artifact_refs collects.
  const refs = new Set<string>();
  for (const segment of verified.value.segments) {
    refsIn(parsed(segment.bytes), refs);
    for (const line of segment.events) refsIn(parsed(line.bytes), refs);
  }
  const files = new Map<string, Uint8Array>();
  for (const sha256 of [...refs].toSorted()) {
    const got = await artifacts.get(sha256);
    if (!got.ok) return got;
    files.set(sha256, got.value);
  }
  return ok({ log: bytes.value, artifacts: files });
}

/** One stored line as JSON; a line that doesn't parse can't be here, since the export verified. */
function parsed(line: Uint8Array): unknown {
  return JSON.parse(new TextDecoder().decode(line));
}

/** `path` claimed by this export alone: an exclusive mkdir, so a racing export loses it. */
function claim(path: string): Result<void, LogError> {
  try {
    mkdirSync(path, { mode: 0o700 });
    return ok(undefined);
  } catch (error) {
    if (code(error) === "EEXIST")
      return err(logError("path_exists", `${path} already exists`));
    return err(ioError(error, path));
  }
}

/** Files, then fsyncs, then the manifest: the bundle exists the instant bundle.json lands. */
function publish(
  path: string,
  branchId: BranchId,
  contents: Contents,
): ExportedBundle {
  durable(join(path, LOG_FILE), contents.log);
  const dir = join(path, ARTIFACTS_DIR);
  mkdirSync(dir, { mode: 0o700 });
  const sizes: Record<string, number> = {};
  for (const [sha256, bytes] of contents.artifacts) {
    durable(join(dir, sha256), bytes);
    sizes[sha256] = bytes.length;
  }
  fsyncDir(dir);
  const manifest: Bundle = {
    format: 1,
    branch_id: branchId,
    log_sha256: sha256Hex(contents.log),
    artifacts: sizes,
  };
  const temp = join(path, `.${MANIFEST}.tmp`);
  durable(temp, encodeBundle(manifest));
  renameSync(temp, join(path, MANIFEST));
  fsyncDir(path);
  fsyncDir(dirname(path));
  return {
    path,
    branch_id: branchId,
    artifacts: contents.artifacts.size,
  };
}

/** One file, written and fsynced before the next. */
function durable(file: string, bytes: Uint8Array): void {
  const fd = openSync(file, "wx", 0o600);
  try {
    writeSync(fd, bytes);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

function code(error: unknown): string | undefined {
  return typeof error === "object" && error !== null && "code" in error
    ? String(error.code)
    : undefined;
}

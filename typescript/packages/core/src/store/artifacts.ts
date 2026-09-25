import { createHash } from "node:crypto";
import {
  closeSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  unlinkSync,
  utimesSync,
  writeSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { sha256Hex } from "../hash";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import { sweepFiles } from "./artifact-sweep";
import {
  artifactSeams,
  codeOf,
  readIfPresent,
  restoreFromTrash,
  seam,
} from "./artifact-trash";
import { StoreError } from "./driver";

/**
 * Content-addressed bytes. `put` returns once the bytes are durable, so an
 * artifact exists before any row or event names it. `get` verifies the hash on every read.
 */
export type ArtifactStore = {
  readonly put: (bytes: Uint8Array) => Promise<string>;
  readonly get: (sha256: string) => Promise<Result<Uint8Array, LogError>>;
  /** Streams one artifact in chunks, so large output is never held whole where it can be. */
  readonly sink: () => ArtifactSink;
  /**
   * `threads gc`: removes every artifact older than `olderThan` (by its write time) that `keep`
   * doesn't name, and returns the removed hashes.
   */
  readonly sweep: (
    keep: ReadonlySet<string>,
    olderThan: number,
  ) => Promise<readonly string[]>;
};

/** One artifact being written. `finish` resolves once it is durable; `abort` drops it. */
export type ArtifactSink = {
  readonly write: (chunk: Uint8Array) => void;
  readonly finish: () => Promise<{
    readonly sha256: string;
    readonly bytes: number;
  }>;
  readonly abort: () => void;
};

export function verified(
  sha256: string,
  bytes: Uint8Array | undefined,
): Result<Uint8Array, LogError> {
  if (bytes === undefined)
    return err(logError("artifact_missing", `no artifact ${sha256}`));
  return sha256Hex(bytes) === sha256
    ? ok(bytes)
    : err(logError("artifact_corrupt", `artifact ${sha256} fails its hash`));
}

/** Artifacts in memory, for tests and in-memory stores. */
export function memoryArtifacts(): ArtifactStore {
  const saved = new Map<string, Uint8Array>();
  const put = (bytes: Uint8Array): string => {
    const sha256 = sha256Hex(bytes);
    saved.set(sha256, bytes.slice());
    return sha256;
  };
  return {
    put: async (bytes) => put(bytes),
    get: async (sha256) => verified(sha256, saved.get(sha256)),
    sweep: async (keep) => {
      const removed = [...saved.keys()].filter((sha256) => !keep.has(sha256));
      for (const sha256 of removed) saved.delete(sha256);
      return removed;
    },
    sink: () => {
      const chunks: Uint8Array[] = [];
      return {
        write: (chunk) => {
          chunks.push(chunk.slice());
        },
        finish: async () => {
          const bytes = new Uint8Array(
            chunks.reduce((n, c) => n + c.length, 0),
          );
          let at = 0;
          for (const c of chunks) {
            bytes.set(c, at);
            at += c.length;
          }
          return { sha256: put(bytes), bytes: bytes.length };
        },
        abort: () => {
          chunks.length = 0;
        },
      };
    },
  };
}

/** Artifacts under `root` as `sha256/<ab>/<64hex>`: directories 0700, files 0600. */
export function fileArtifacts(root: string): ArtifactStore {
  const path = (sha256: string): string =>
    join(root, "sha256", sha256.slice(0, 2), sha256);
  const sink = (): ArtifactSink => {
    const s = disk(() => fileSink(root, path));
    return {
      write: (chunk) => disk(() => s.write(chunk)),
      finish: async () => disk(() => s.finish()),
      abort: () => disk(() => s.abort()),
    };
  };
  return {
    put: async (bytes) => {
      const s = sink();
      s.write(bytes);
      return (await s.finish()).sha256;
    },
    // A missing or changed artifact stays a value; a disk that fails is the store's outage.
    get: async (sha256) =>
      verified(
        sha256,
        disk(() => readArtifact(path(sha256))),
      ),
    sink,
    sweep: async (keep, olderThan) =>
      disk(() => sweepFiles(root, keep, olderThan)),
  };
}

/** The file system's errors under the artifacts, raised as the store's outage. */
function disk<T>(run: () => T): T {
  try {
    return run();
  } catch (error) {
    if (error instanceof Error && "syscall" in error)
      throw new StoreError(error.message, { cause: error });
    throw error;
  }
}

/**
 * Writes a temp file chunk by chunk while hashing, then fsyncs it and links it to its
 * content address (EEXIST: keep the existing copy if it verifies), then fsyncs the directory.
 */
/** A file being written, synchronously: the file store's sink before it is wrapped. */
type FileSink = {
  readonly write: (chunk: Uint8Array) => void;
  readonly finish: () => { readonly sha256: string; readonly bytes: number };
  readonly abort: () => void;
};

function fileSink(root: string, path: (sha256: string) => string): FileSink {
  const tmp = join(root, "sha256");
  mkdirSync(tmp, { recursive: true, mode: 0o700 });
  const temp = join(tmp, `.${crypto.randomUUID()}.tmp`);
  const fd = openSync(temp, "wx", 0o600);
  const hash = createHash("sha256");
  let size = 0;
  let open = true;
  const close = (): void => {
    if (open) closeSync(fd);
    open = false;
  };
  return {
    write: (chunk) => {
      writeSync(fd, chunk);
      hash.update(chunk);
      size += chunk.length;
    },
    finish: () => {
      fsyncSync(fd);
      close();
      const sha256 = hash.digest("hex");
      const dir = join(root, "sha256", sha256.slice(0, 2));
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      try {
        place(temp, path(sha256), sha256);
      } finally {
        unlinkSync(temp);
      }
      fsyncDir(dir);
      return { sha256, bytes: size };
    },
    abort: () => {
      close();
      unlinkSync(temp);
    },
  };
}

function fsyncDir(dir: string): void {
  const fd = openSync(dir, "r");
  try {
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

const ROUNDS = 3;

/**
 * Links the temp file to its content address. An existing copy is refreshed (mtime now, so a
 * gc grace counts from this put) and then verified. If a concurrent gc moved it to trash
 * meanwhile (ENOENT), the temp file, still ours, is linked again; the new link is a name gc
 * never deletes (spec/schema/README.md, "Artifacts", rule 1).
 */
function place(temp: string, path: string, sha256: string): void {
  for (let round = 0; round < ROUNDS; round++) {
    try {
      linkSync(temp, path);
      return;
    } catch (error) {
      if (codeOf(error) !== "EEXIST") throw error;
    }
    seam("put:exists");
    if (artifactSeams.rules && !refreshed(path)) continue;
    seam("put:verify");
    const bytes = readIfPresent(path);
    if (bytes === undefined && artifactSeams.rules) continue;
    if (!verified(sha256, bytes).ok)
      throw new StoreError(
        `artifact ${sha256}: the existing copy fails its hash`,
      );
    return;
  }
  throw new StoreError(`artifact ${sha256} kept vanishing while it was stored`);
}

/** Sets the existing copy's mtime to now; false when it vanished. */
function refreshed(path: string): boolean {
  const now = new Date();
  try {
    utimesSync(path, now, now);
    return true;
  } catch (error) {
    if (codeOf(error) === "ENOENT") return false;
    throw error;
  }
}

/** The artifact's bytes, restored from a gc's trash name when its content path is gone. */
function readArtifact(path: string): Uint8Array | undefined {
  return (
    readIfPresent(path) ??
    restoreFromTrash(dirname(path), path.slice(-64), path)
  );
}

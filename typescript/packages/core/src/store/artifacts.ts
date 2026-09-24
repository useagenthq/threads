import { createHash } from "node:crypto";
import {
  closeSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { join } from "node:path";
import { sha256Hex } from "../hash";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import { StoreError } from "./driver";

/**
 * Content-addressed bytes. `put` returns once the bytes are durable, so an
 * artifact exists before any row or event names it. `get` verifies the hash on every read.
 */
export type ArtifactStore = {
  readonly put: (bytes: Uint8Array) => string;
  readonly get: (sha256: string) => Result<Uint8Array, LogError>;
  /** Streams one artifact in chunks, so large output is never held whole. */
  readonly sink: () => ArtifactSink;
};

/** One artifact being written. `finish` returns once it is durable; `abort` drops it. */
export type ArtifactSink = {
  readonly write: (chunk: Uint8Array) => void;
  readonly finish: () => { readonly sha256: string; readonly bytes: number };
  readonly abort: () => void;
};

function verified(
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
    put,
    get: (sha256) => verified(sha256, saved.get(sha256)),
    sink: () => {
      const chunks: Uint8Array[] = [];
      return {
        write: (chunk) => {
          chunks.push(chunk.slice());
        },
        finish: () => {
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
      finish: () => disk(() => s.finish()),
      abort: () => disk(() => s.abort()),
    };
  };
  return {
    put: (bytes) => {
      const s = sink();
      s.write(bytes);
      return s.finish().sha256;
    },
    // A missing or changed artifact stays a value; a disk that fails is the store's outage.
    get: (sha256) =>
      verified(
        sha256,
        disk(() => readArtifact(path(sha256))),
      ),
    sink,
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
function fileSink(
  root: string,
  path: (sha256: string) => string,
): ArtifactSink {
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
        linkSync(temp, path(sha256));
      } catch (error) {
        if (!isExists(error)) throw error;
        if (!verified(sha256, readArtifact(path(sha256))).ok) throw error;
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

function readArtifact(path: string): Uint8Array | undefined {
  try {
    return new Uint8Array(readFileSync(path));
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT")
      return undefined;
    throw error;
  }
}

function isExists(error: unknown): boolean {
  return error instanceof Error && "code" in error && error.code === "EEXIST";
}

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

/**
 * Content-addressed bytes. `put` returns once the bytes are durable, so an
 * artifact exists before any row or event names it. `get` verifies the hash on every read.
 */
export type ArtifactStore = {
  readonly put: (bytes: Uint8Array) => string;
  readonly get: (sha256: string) => Result<Uint8Array, LogError>;
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
  return {
    put: (bytes) => {
      const sha256 = sha256Hex(bytes);
      saved.set(sha256, bytes.slice());
      return sha256;
    },
    get: (sha256) => verified(sha256, saved.get(sha256)),
  };
}

/** Artifacts under `root` as `sha256/<ab>/<64hex>`: directories 0700, files 0600. */
export function fileArtifacts(root: string): ArtifactStore {
  const path = (sha256: string): string =>
    join(root, "sha256", sha256.slice(0, 2), sha256);
  return {
    put: (bytes) => {
      const sha256 = sha256Hex(bytes);
      const dir = join(root, "sha256", sha256.slice(0, 2));
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      const temp = join(dir, `.${sha256}.${process.pid}.${Date.now()}.tmp`);
      writeDurably(temp, bytes);
      try {
        linkSync(temp, path(sha256));
      } catch (error) {
        // Another writer stored it first: keep theirs if it is intact.
        if (!isExists(error)) throw error;
        const existing = readArtifact(path(sha256));
        if (!verified(sha256, existing).ok) throw error;
      } finally {
        unlinkSync(temp);
      }
      fsyncDir(dir);
      return sha256;
    },
    get: (sha256) => verified(sha256, readArtifact(path(sha256))),
  };
}

function writeDurably(path: string, bytes: Uint8Array): void {
  const fd = openSync(path, "wx", 0o600);
  try {
    writeSync(fd, bytes);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
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

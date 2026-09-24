import { linkSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { sha256Hex } from "../hash";

// The race rules that keep a shared artifact from being lost to a concurrent `threads gc`
// (spec/schema/README.md, "Artifacts"): gc only ever unlinks a trash name, and whoever needs
// the file links it back. These helpers are shared by the store (artifacts.ts) and gc
// (retention.ts).

/** The file-system steps a test can interleave with (see `artifactSeams`). */
export type SeamPoint =
  | "put:exists"
  | "put:verify"
  | "gc:stated"
  | "gc:renamed";

/**
 * Test-only: `hook` runs at each seam point, so a test forces one interleaving
 * deterministically; `rules: false` restores the old behaviour (no refresh, re-link, trash or
 * restore) for the control tests. Never set outside tests.
 */
export const artifactSeams: {
  hook: ((point: SeamPoint) => void) | undefined;
  rules: boolean;
} = { hook: undefined, rules: true };

export function seam(point: SeamPoint): void {
  artifactSeams.hook?.(point);
}

/** `.<sha256>.trash-`: the prefix of a trash name in the artifact's own directory. */
export function trashPrefix(sha256: string): string {
  return `.${sha256}.trash-`;
}

/** A fresh trash name for `sha256`: renaming to it is atomic and keeps the inode and mtime. */
export function trashName(sha256: string): string {
  return `${trashPrefix(sha256)}${crypto.randomUUID()}`;
}

/** Links `from` to `to`; true when `to` exists afterwards, false when `from` vanished. */
export function linkBack(from: string, to: string): boolean {
  try {
    linkSync(from, to);
    return true;
  } catch (error) {
    if (codeOf(error) === "EEXIST") return true;
    if (codeOf(error) === "ENOENT") return false;
    throw error;
  }
}

/**
 * Rule 3: a reader that finds no file restores it from a trash name that verifies, linked
 * back to the content path (the link shares the inode, so gc unlinking the trash later keeps
 * it). Returns the bytes, or undefined when no trash holds them.
 */
export function restoreFromTrash(
  dir: string,
  sha256: string,
  path: string,
): Uint8Array | undefined {
  if (!artifactSeams.rules) return undefined;
  const prefix = trashPrefix(sha256);
  for (const name of listDir(dir).filter((n) => n.startsWith(prefix))) {
    const trash = join(dir, name);
    const bytes = readIfPresent(trash);
    if (bytes === undefined || sha256Hex(bytes) !== sha256) continue;
    if (linkBack(trash, path)) return bytes;
  }
  return undefined;
}

export function readIfPresent(path: string): Uint8Array | undefined {
  try {
    return new Uint8Array(readFileSync(path));
  } catch (error) {
    if (codeOf(error) === "ENOENT") return undefined;
    throw error;
  }
}

/** A directory's names; none when it is gone or not a directory (a temp file beside them). */
export function listDir(dir: string): readonly string[] {
  try {
    return readdirSync(dir);
  } catch (error) {
    if (codeOf(error) === "ENOENT" || codeOf(error) === "ENOTDIR") return [];
    throw error;
  }
}

export function codeOf(error: unknown): unknown {
  return error instanceof Error && "code" in error ? error.code : undefined;
}

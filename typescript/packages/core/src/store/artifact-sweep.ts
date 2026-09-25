import { renameSync, rmSync, statSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import {
  artifactSeams,
  codeOf,
  linkBack,
  listDir,
  seam,
  trashName,
} from "./artifact-trash";

// The file artifacts' half of `threads gc`: removing what nothing references, by trash names.

const TRASH = /^\.([0-9a-f]{64})\.trash-/;

/**
 * The sweep over the files, given what is referenced. A file is only ever moved to a trash
 * name, re-checked and then unlinked by that name, so a concurrent put or get can restore it
 * (spec/schema/README.md, "Artifacts", rule 2).
 */
export function sweepFiles(
  root: string,
  kept: ReadonlySet<string>,
  olderThan: number,
): readonly string[] {
  const removed: string[] = [];
  const base = join(root, "sha256");
  for (const prefix of listDir(base)) {
    const dir = join(base, prefix);
    for (const name of listDir(dir)) {
      const trash = TRASH.exec(name)?.[1];
      const gone =
        trash === undefined
          ? !kept.has(name) && collect(dir, name, olderThan)
          : leftover(dir, name, trash, kept, olderThan);
      if (gone) removed.push(trash ?? name);
    }
  }
  return removed;
}

/** One unreferenced candidate: true when it was deleted. */
function collect(dir: string, name: string, olderThan: number): boolean {
  const file = join(dir, name);
  if (!old(file, olderThan)) return false;
  seam("gc:stated");
  if (!artifactSeams.rules) {
    rmSync(file);
    return true;
  }
  const trash = join(dir, trashName(name));
  try {
    renameSync(file, trash);
  } catch (error) {
    if (codeOf(error) === "ENOENT") return false;
    throw error;
  }
  seam("gc:renamed");
  if (old(trash, olderThan)) return unlinked(trash);
  // A put refreshed it before the rename: it is wanted again.
  linkBack(trash, file);
  unlinked(trash);
  return false;
}

/**
 * A trash name a gc left behind when it stopped between rename and unlink, removed once it is
 * older than the grace. A referenced one is linked back first, so no named artifact is lost.
 */
function leftover(
  dir: string,
  name: string,
  sha256: string,
  kept: ReadonlySet<string>,
  olderThan: number,
): boolean {
  const trash = join(dir, name);
  if (!old(trash, olderThan)) return false;
  const file = join(dir, sha256);
  if (kept.has(sha256)) linkBack(trash, file);
  return unlinked(trash) && !exists(file);
}

/** Older than the grace; a file that vanished meanwhile is not a candidate. */
function old(path: string, olderThan: number): boolean {
  try {
    return statSync(path).mtimeMs < olderThan;
  } catch (error) {
    if (codeOf(error) === "ENOENT") return false;
    throw error;
  }
}

function exists(path: string): boolean {
  try {
    statSync(path);
    return true;
  } catch (error) {
    if (codeOf(error) === "ENOENT") return false;
    throw error;
  }
}

/** Unlinks a trash name; false when another process got there first. */
function unlinked(path: string): boolean {
  try {
    unlinkSync(path);
    return true;
  } catch (error) {
    if (codeOf(error) === "ENOENT") return false;
    throw error;
  }
}

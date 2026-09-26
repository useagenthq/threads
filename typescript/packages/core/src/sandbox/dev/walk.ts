import {
  closeSync,
  constants,
  type Dirent,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { err, ok, type Result } from "../../result";
import type { FileFailure } from "../protocol";
import { sandboxPath, WORKSPACE } from "../remote/scripts";

// Every host-side file operation of the dev sandbox walks the path one component at a time and
// refuses a component that is a symlink, then opens the last one with O_NOFOLLOW. A lexical
// check can't do this: `a/b -> ..` followed by `c -> a/b/..` leaves the root through two links
// that each look harmless. A symlink is only ever read as a link here, never traversed. Inside
// the confinement the command may follow links freely: that is the sandbox's own filesystem.
//
// ponytail: Node has no openat, so a component is checked with lstat and the last is opened
// with O_NOFOLLOW; a component swapped between the check and the open is still possible.
// Python's side does the real openat walk (adapters/sandboxes/dev/walk.py).

const NOFOLLOW = constants.O_NOFOLLOW;

const CODES: ReadonlyMap<string, FileFailure["code"]> = new Map([
  ["ENOENT", "not_found"],
  ["ENOTDIR", "not_found"],
  ["EISDIR", "is_directory"],
  ["ELOOP", "invalid_path"],
  ["EMLINK", "invalid_path"],
  ["EACCES", "permission_denied"],
  ["EPERM", "permission_denied"],
  ["EFBIG", "too_large"],
]);

function codeOf(error: unknown): string {
  if (error === null || typeof error !== "object" || !("code" in error))
    return "";
  return typeof error.code === "string" ? error.code : "";
}

/** A host errno as the protocol's file failure. */
export function failed(error: unknown, path: string): FileFailure {
  const errno = codeOf(error);
  return {
    code: CODES.get(errno) ?? "unavailable",
    message: `${path}: ${errno === "" ? String(error) : errno}`,
  };
}

const linked = (path: string): FileFailure => ({
  code: "invalid_path",
  message: `${path}: a symlink is never followed on the host`,
});

/**
 * The components of `path` under /workspace, or a typed failure: a path outside /workspace is
 * outside the sandbox, because the dev sandbox's filesystem is the host's.
 */
export function workspaceParts(
  path: string,
): Result<readonly string[], FileFailure> {
  const at = sandboxPath(path);
  if (at === undefined || (at !== WORKSPACE && !at.startsWith(`${WORKSPACE}/`)))
    return err({
      code: "invalid_path",
      message: `${path}: the dev sandbox holds only ${WORKSPACE}`,
    });
  return ok(
    at
      .slice(WORKSPACE.length)
      .split("/")
      .filter((part) => part !== ""),
  );
}

/**
 * `dir` joined with `parts`, every component before the last proven to be a real directory.
 * `create` makes a missing one (0o755), as the kits' write check does.
 */
export function hostPath(
  dir: string,
  parts: readonly string[],
  create: boolean,
): Result<string, FileFailure> {
  let at = dir;
  for (const part of parts.slice(0, -1)) {
    at = `${at}/${part}`;
    let stat: ReturnType<typeof lstatSync>;
    try {
      stat = lstatSync(at);
    } catch (error) {
      if (!create || codeOf(error) !== "ENOENT") return err(failed(error, at));
      try {
        mkdirSync(at, 0o755);
      } catch (made) {
        return err(failed(made, at));
      }
      continue;
    }
    if (stat.isSymbolicLink()) return err(linked(at));
    if (!stat.isDirectory())
      return err({ code: "not_found", message: `${at}: not a directory` });
  }
  const last = parts.at(-1);
  return ok(last === undefined ? at : `${at}/${last}`);
}

/** The file's bytes, refusing a symlink at any component. */
export function readIn(
  dir: string,
  parts: readonly string[],
): Result<Uint8Array, FileFailure> {
  const at = hostPath(dir, parts, false);
  if (!at.ok) return at;
  let fd: number | undefined;
  try {
    fd = openSync(at.value, constants.O_RDONLY | NOFOLLOW);
    return ok(new Uint8Array(readFileSync(fd)));
  } catch (error) {
    return err(failed(error, at.value));
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

/** Writes the file, making missing parents, refusing a symlink at any component. */
export function writeIn(
  dir: string,
  parts: readonly string[],
  data: Uint8Array,
  mode: number,
): Result<void, FileFailure> {
  const at = hostPath(dir, parts, true);
  if (!at.ok) return at;
  let fd: number | undefined;
  try {
    const flags =
      constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | NOFOLLOW;
    fd = openSync(at.value, flags, mode);
    writeFileSync(fd, data);
    return ok(undefined);
  } catch (error) {
    return err(failed(error, at.value));
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

/** Makes the directory, making missing parents, refusing a symlink at any component. */
export function mkdirIn(
  dir: string,
  parts: readonly string[],
  mode: number,
): Result<void, FileFailure> {
  const at = hostPath(dir, parts, true);
  if (!at.ok) return at;
  try {
    mkdirSync(at.value, mode);
    return ok(undefined);
  } catch (error) {
    if (codeOf(error) === "EEXIST") return ok(undefined);
    return err(failed(error, at.value));
  }
}

/** Makes the symlink, refusing a symlink at any component of its own path. */
export function symlinkIn(
  dir: string,
  parts: readonly string[],
  target: string,
): Result<void, FileFailure> {
  const at = hostPath(dir, parts, true);
  if (!at.ok) return at;
  try {
    symlinkSync(target, at.value);
    return ok(undefined);
  } catch (error) {
    return err(failed(error, at.value));
  }
}

/** One thing found in the tree, by its path relative to the sandbox's /workspace. */
export type Found =
  | { readonly path: string; readonly kind: "dir"; readonly mode: number }
  | {
      readonly path: string;
      readonly kind: "file";
      readonly mode: number;
      readonly size: number;
      readonly host: string;
    }
  | {
      readonly path: string;
      readonly kind: "symlink";
      readonly target: string;
    };

function entryOf(dir: string, at: string, e: Dirent): Found | FileFailure {
  const host = `${dir}/${at}`;
  if (e.isSymbolicLink())
    return { path: at, kind: "symlink", target: readlinkSync(host) };
  const stat = lstatSync(host);
  const mode = stat.mode & 0o7777;
  if (e.isDirectory()) return { path: at, kind: "dir", mode };
  if (e.isFile())
    return { path: at, kind: "file", mode, size: stat.size, host };
  return {
    code: "invalid_path",
    message: `${host}: not a file, directory or symlink`,
  };
}

/**
 * Everything under `dir`, breadth by directory, sorted by path, never descending into a
 * symlink. Each entry's host path is read back through this same walk, so nothing outside the
 * root is ever opened.
 */
export function walkTree(dir: string): Result<readonly Found[], FileFailure> {
  const out: Found[] = [];
  const queue: string[] = [""];
  try {
    for (const prefix of queue) {
      const here = prefix === "" ? dir : `${dir}/${prefix}`;
      for (const e of readdirSync(here, { withFileTypes: true }).toSorted(
        (a, b) => (a.name < b.name ? -1 : 1),
      )) {
        const at = prefix === "" ? e.name : `${prefix}/${e.name}`;
        const found = entryOf(dir, at, e);
        if ("code" in found) return err(found);
        out.push(found);
        if (found.kind === "dir") queue.push(at);
      }
    }
  } catch (error) {
    return err(failed(error, dir));
  }
  return ok(out.toSorted((a, b) => (a.path < b.path ? -1 : 1)));
}

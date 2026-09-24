// Path rules shared by the archive reader and the tree parser (spec/schema/README.md,
// "Snapshot manifest"). Paths are relative to the root, "/"-separated; `\` is an ordinary
// character.

export type Kind = "file" | "dir" | "symlink";

// Fatal: a lossy decode would give two different byte paths one name. ignoreBOM keeps a
// leading U+FEFF as part of the name, as Python's decoder does.
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/** The bytes as UTF-8 text; undefined when they aren't UTF-8. */
export function decodeUtf8(bytes: Uint8Array): string | undefined {
  try {
    return utf8.decode(bytes);
  } catch {
    return undefined;
  }
}

/** Every component is a real name: no empty, `.` or `..` component, and no NUL. */
export function isNormal(path: string): boolean {
  return (
    !path.includes("\0") &&
    path.split("/").every((c) => c !== "" && c !== "." && c !== "..")
  );
}

/**
 * An archive name as the tree names it: one leading `./` stripped, then one trailing `/` of a
 * directory. "" is the root. Undefined: absolute, or not normal.
 */
export function normalize(raw: string, dir: boolean): string | undefined {
  if (raw.startsWith("/")) return undefined;
  let path = raw.startsWith("./") ? raw.slice(2) : raw;
  if (dir && path.endsWith("/")) path = path.slice(0, -1);
  if (path === "" || path === ".") return "";
  return isNormal(path) ? path : undefined;
}

/** A symlink at `path` whose target stays in the root lexically, resolved from its directory. */
export function inRoot(path: string, target: string): boolean {
  if (target === "" || target.startsWith("/") || target.includes("\0"))
    return false;
  const at = path.split("/").slice(0, -1);
  for (const part of target.split("/")) {
    if (part === "" || part === ".") continue;
    if (part !== "..") at.push(part);
    else if (at.pop() === undefined) return false;
  }
  return true;
}

/**
 * The paths of one tree, added in order. A path already present is a duplicate. A path with a
 * file or symlink above it, or a file or symlink with anything below it, is a conflict: its
 * extraction would write through a link or into a file.
 */
export function pathSet(): {
  readonly add: (
    path: string,
    kind: Kind,
  ) => "duplicate" | "path_conflict" | undefined;
  readonly kind: (path: string) => Kind | undefined;
} {
  const kinds = new Map<string, Kind>();
  const parents = new Set<string>();
  return {
    kind: (path) => kinds.get(path),
    add: (path, kind) => {
      if (kinds.has(path)) return "duplicate";
      if (kind !== "dir" && parents.has(path)) return "path_conflict";
      const above = path
        .split("/")
        .slice(0, -1)
        .map((_, i, parts) => parts.slice(0, i + 1).join("/"));
      if (above.some((p) => (kinds.get(p) ?? "dir") !== "dir"))
        return "path_conflict";
      for (const p of above) parents.add(p);
      kinds.set(path, kind);
      return undefined;
    },
  };
}

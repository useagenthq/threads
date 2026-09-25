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

/** The longest path, in UTF-8 bytes (Linux PATH_MAX): it keeps every path check bounded. */
export const MAX_PATH = 4096;
const encoder = new TextEncoder();

/** At most MAX_PATH bytes, and every component a real name: no empty, `.` or `..`, no NUL. */
export function isNormal(path: string): boolean {
  return (
    // A UTF-8 byte is at least a quarter of a UTF-16 unit; skip encoding a huge path.
    path.length <= MAX_PATH &&
    encoder.encode(path).length <= MAX_PATH &&
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

/** A path component's node: its own kind once added (undefined: only an implied directory). */
type Node = { kind: Kind | undefined; readonly children: Map<string, Node> };

const isLink = (node: Node): boolean => (node.kind ?? "dir") !== "dir";

/**
 * The parent directory's node, creating implied directories; undefined when a file or symlink
 * is on the way. A file or symlink never has children, so a refusal has created nothing.
 */
function parent(root: Node, parts: readonly string[]): Node | undefined {
  let node = root;
  for (const part of parts) {
    if (isLink(node)) return undefined;
    const next = node.children.get(part) ?? {
      kind: undefined,
      children: new Map(),
    };
    node.children.set(part, next);
    node = next;
  }
  return isLink(node) ? undefined : node;
}

/**
 * The paths of one tree, added in order, as a tree of components so each add is linear in its
 * path. A path already present is a duplicate. A path with a file or symlink above it, or a
 * file or symlink with anything below it, is a conflict: its extraction would write through a
 * link or into a file.
 */
export function pathSet(): {
  readonly add: (
    path: string,
    kind: Kind,
  ) => "duplicate" | "path_conflict" | undefined;
} {
  const root: Node = { kind: "dir", children: new Map() };
  return {
    add: (path, kind) => {
      const parts = path.split("/");
      const last = parts.pop() ?? "";
      const dir = parent(root, parts);
      if (dir === undefined) return "path_conflict";
      const found = dir.children.get(last);
      if (found === undefined) {
        dir.children.set(last, { kind, children: new Map() });
        return undefined;
      }
      if (found.kind !== undefined) return "duplicate";
      if (kind !== "dir") return "path_conflict";
      found.kind = kind;
      return undefined;
    },
  };
}

import { err, ok, type Result } from "../../result";
import type { ArtifactSink, ArtifactStore } from "../../store/artifacts";
import type { Failure } from "../protocol";
import {
  BLOCK,
  type Header,
  isZero,
  type Pax,
  parseHeader,
  parsePax,
} from "./header";
import { decodeUtf8, inRoot, normalize, pathSet } from "./paths";
import { Source } from "./source";
import {
  encodeTree,
  type Tree,
  type TreeEntry,
  treeManifestHash,
} from "./tree";

// The streaming reader for untrusted tar archives (spec/schema/README.md, "Snapshot manifest").
// It pulls exact byte counts from the source, so any chunking gives the same result; each
// regular file streams into its own artifact as it arrives.

export type Caps = { readonly file: number; readonly total: number };
/** 256 MiB per file, 1 GiB per archive stream. */
export const CAPS: Caps = { file: 256 * 2 ** 20, total: 2 ** 30 };
/** The most bytes of one pax or GNU long-name header's data. */
const META = 2 ** 20;

export type ArchiveReason =
  | "bad_header"
  | "truncated"
  | "bad_path"
  | "unsupported_type"
  | "file_too_large"
  | "archive_too_large"
  | "bad_symlink"
  | "bad_hardlink"
  | "duplicate"
  | "path_conflict";

/** Why an archive was refused, naming its entry (null when no entry's name was read). */
export type ArchiveInvalid = Failure<"archive_invalid"> & {
  readonly reason: ArchiveReason;
  readonly entry: string | null;
};

function invalid(
  reason: ArchiveReason,
  entry: string | null,
  offset: number,
): { readonly ok: false; readonly error: ArchiveInvalid } {
  const where =
    entry === null ? `at byte ${offset}` : `entry ${JSON.stringify(entry)}`;
  return err({
    code: "archive_invalid",
    reason,
    entry,
    message: `archive ${where}: ${reason.replaceAll("_", " ")}`,
  });
}

const padding = (size: number): number => (BLOCK - (size % BLOCK)) % BLOCK;
const skip = (): void => {};

/** The long names and pax records that apply to the next entry. */
type Pending = {
  readonly pax?: Pax;
  readonly name?: Uint8Array;
  readonly link?: Uint8Array;
};
type State = {
  readonly src: Source;
  readonly caps: Caps;
  readonly open: () => ArtifactSink;
  readonly paths: ReturnType<typeof pathSet>;
  /** By path, in archive order. */
  readonly entries: Map<string, TreeEntry>;
};
type Step = Result<Pending, ArchiveInvalid>;

/** A pax ('x'), GNU long name ('L') or long link ('K') header: its data names the next entry. */
async function meta(
  s: State,
  h: Header,
  pending: Pending,
  at: number,
): Promise<Step> {
  const slot = h.type === "x" ? "pax" : h.type === "L" ? "name" : "link";
  if (pending[slot] !== undefined || h.size > META)
    return invalid("bad_header", null, at);
  if (s.src.offset + h.size + padding(h.size) > s.caps.total)
    return invalid("archive_too_large", null, at);
  const data = await s.src.exact(h.size);
  if (data === undefined || !(await s.src.take(padding(h.size), skip)))
    return invalid("truncated", null, s.src.offset);
  if (slot !== "pax") {
    const nul = data.indexOf(0);
    return ok({
      ...pending,
      [slot]: nul === -1 ? data : data.subarray(0, nul),
    });
  }
  const pax = parsePax(data);
  return pax === undefined
    ? invalid("bad_header", null, at)
    : ok({ ...pending, pax });
}

const KINDS: Readonly<Record<string, "file" | "dir" | "symlink" | "hardlink">> =
  {
    "0": "file",
    "\0": "file",
    "5": "dir",
    "2": "symlink",
    "1": "hardlink",
  };

/** A file's bytes into a new artifact; its tree entry, or why the archive is refused. */
async function file(
  s: State,
  path: string,
  size: number,
  mode: number,
): Promise<Result<TreeEntry, ArchiveInvalid>> {
  const sink = s.open();
  const whole =
    (await s.src.take(size, (piece) => sink.write(piece))) &&
    (await s.src.take(padding(size), skip));
  if (!whole) {
    sink.abort();
    return invalid("truncated", path, s.src.offset);
  }
  return ok({ path, kind: "file", mode, size, sha256: sink.finish().sha256 });
}

/** The entry a link or directory header describes, or why it is refused. */
function linked(
  s: State,
  kind: "dir" | "symlink" | "hardlink",
  path: string,
  h: Header,
  link: Uint8Array,
): TreeEntry | "bad_symlink" | "bad_hardlink" {
  if (kind === "dir") return { path, kind, mode: h.mode };
  const target = decodeUtf8(link);
  if (kind === "symlink")
    return target !== undefined && inRoot(path, target)
      ? { path, kind, target }
      : "bad_symlink";
  // Expanded: a regular file with its target's content and mode, as `find -type f` sees it.
  const earlier = s.entries.get(normalize(target ?? "", false) ?? "");
  return earlier?.kind === "file" ? { ...earlier, path } : "bad_hardlink";
}

type Named = {
  readonly path: string;
  readonly kind: "file" | "dir" | "symlink" | "hardlink";
  readonly size: number;
};

/** The entry's normalized path ("" for the root), kind and data size. */
function named(
  h: Header,
  pending: Pending,
  at: number,
): Result<Named, ArchiveInvalid> {
  const raw = decodeUtf8(pending.pax?.path ?? pending.name ?? h.name);
  if (raw === undefined) return invalid("bad_path", null, at);
  const kind = KINDS[h.type];
  const path = normalize(raw, kind === "dir");
  if (path === undefined || (path === "" && kind !== "dir"))
    return invalid("bad_path", raw, at);
  if (kind === undefined) return invalid("unsupported_type", path, at);
  const size = pending.pax?.size ?? h.size;
  if (kind !== "file" && size !== 0) return invalid("bad_header", path, at);
  return ok({ path, kind, size });
}

/** One real entry: its checks in order, then its data. */
async function entry(
  s: State,
  h: Header,
  pending: Pending,
  at: number,
): Promise<Step> {
  const names = named(h, pending, at);
  if (!names.ok) return names;
  const { path, kind, size } = names.value;
  if (path === "") return ok({});
  if (size > s.caps.file) return invalid("file_too_large", path, at);
  if (s.src.offset + size + padding(size) > s.caps.total)
    return invalid("archive_too_large", path, at);
  const link = pending.pax?.linkpath ?? pending.link ?? h.link;
  const made = kind === "file" ? undefined : linked(s, kind, path, h, link);
  if (typeof made === "string") return invalid(made, path, at);
  const clash = s.paths.add(path, made?.kind ?? "file");
  if (clash !== undefined) return invalid(clash, path, at);
  const stored =
    made === undefined ? await file(s, path, size, h.mode) : ok(made);
  if (!stored.ok) return stored;
  s.entries.set(path, stored.value);
  return ok({});
}

/** After the first zero block: a second one, then only zeros to the end of the source. */
async function end(
  s: State,
  pending: Pending,
): Promise<Result<Tree, ArchiveInvalid>> {
  const at = s.src.offset;
  if (Object.keys(pending).length > 0) return invalid("bad_header", null, at);
  if (at + BLOCK > s.caps.total) return invalid("archive_too_large", null, at);
  const second = await s.src.exact(BLOCK);
  if (second === undefined) return invalid("truncated", null, s.src.offset);
  if (!isZero(second)) return invalid("bad_header", null, at);
  for (;;) {
    const start = s.src.offset;
    const piece = await s.src.next(BLOCK * 64);
    if (piece === undefined) break;
    const inside = piece.subarray(0, Math.max(0, s.caps.total - start));
    const stray = inside.findIndex((b) => b !== 0);
    if (stray !== -1) return invalid("bad_header", null, start + stray);
    if (inside.length < piece.length)
      return invalid("archive_too_large", null, s.caps.total);
  }
  return ok({
    tree_version: 1,
    entries: [...s.entries.values()].toSorted((a, b) =>
      a.path < b.path ? -1 : a.path > b.path ? 1 : 0,
    ),
  });
}

/**
 * Reads an untrusted tar stream into a tree, each regular file streamed into an artifact from
 * `open`. A refused archive may leave the files it already stored, unreferenced.
 */
export async function readTar(
  source: AsyncIterable<Uint8Array>,
  open: () => ArtifactSink,
  caps: Caps = CAPS,
): Promise<Result<Tree, ArchiveInvalid>> {
  const s: State = {
    src: new Source(source),
    caps,
    open,
    paths: pathSet(),
    entries: new Map(),
  };
  let pending: Pending = {};
  try {
    for (;;) {
      const at = s.src.offset;
      if (at + BLOCK > caps.total)
        return invalid("archive_too_large", null, at);
      const block = await s.src.exact(BLOCK);
      if (block === undefined) return invalid("truncated", null, s.src.offset);
      if (isZero(block)) return await end(s, pending);
      const h = parseHeader(block);
      if (h === undefined) return invalid("bad_header", null, at);
      const step =
        h.type === "x" || h.type === "L" || h.type === "K"
          ? await meta(s, h, pending, at)
          : await entry(s, h, pending, at);
      if (!step.ok) return step;
      pending = step.value;
    }
  } finally {
    await s.src.close();
  }
}

/** A stored tree: its artifact, and the manifest hash of its files. */
export type StoredTree = {
  readonly tree: Tree;
  readonly sha256: string;
  readonly manifest_hash: string;
};

/** Reads the archive into `artifacts`: the files first, then the tree artifact that lists them. */
export async function storeTar(
  source: AsyncIterable<Uint8Array>,
  artifacts: ArtifactStore,
  caps: Caps = CAPS,
): Promise<Result<StoredTree, ArchiveInvalid>> {
  const tree = await readTar(source, artifacts.sink, caps);
  if (!tree.ok) return tree;
  return ok({
    tree: tree.value,
    sha256: artifacts.put(encodeTree(tree.value)),
    manifest_hash: treeManifestHash(tree.value),
  });
}

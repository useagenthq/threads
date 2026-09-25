import { err, ok, type Result } from "../../result";
import type { ArtifactStore } from "../../store/artifacts";
import { type LogError, logError } from "../../verify/error";
import { BLOCK } from "./header";
import { broken, type Tree, type TreeEntry } from "./tree";

// The canonical archive of a tree (spec/schema/README.md, "Snapshot manifest"): ustar headers
// in tree order, a pax header only for a name or link over 100 bytes, mtime 0, the caller's
// uid/gid. A tree's paths are normal and its symlinks stay in the root, so extracting the
// archive can't write outside it.

/** The owner every entry is written with (1000 for Docker's command user). */
export type Owner = { readonly uid: number; readonly gid: number };

const utf8 = new TextEncoder();
const FIELD = 100;
const MAX_ID = 0o7777777;

/** `value` as `digits` octal digits plus a NUL, at `at`. */
function octal(
  block: Uint8Array,
  at: number,
  digits: number,
  value: number,
): void {
  // A tree's files are under the 256 MiB cap, so a size never overflows its field.
  if (value >= 8 ** digits)
    throw new Error(`${value} doesn't fit ${digits} octal digits`);
  block.set(utf8.encode(`${value.toString(8).padStart(digits, "0")}\0`), at);
}

function header(
  type: string,
  name: Uint8Array,
  link: Uint8Array,
  mode: number,
  size: number,
  owner: Owner,
): Uint8Array {
  const block = new Uint8Array(BLOCK);
  block.set(name.subarray(0, FIELD), 0);
  octal(block, 100, 7, mode);
  octal(block, 108, 7, owner.uid);
  octal(block, 116, 7, owner.gid);
  octal(block, 124, 11, size);
  octal(block, 136, 11, 0);
  block.set(utf8.encode(type), 156);
  block.set(link.subarray(0, FIELD), 157);
  block.set(utf8.encode("ustar\u000000"), 257);
  octal(block, 329, 7, 0);
  octal(block, 337, 7, 0);
  block.fill(0x20, 148, 156);
  const sum = block.reduce((n, b) => n + b, 0);
  block.set(utf8.encode(`${sum.toString(8).padStart(6, "0")}\0 `), 148);
  return block;
}

/** One pax record; its length counts its own digits. */
function paxRecord(key: string, value: Uint8Array): Uint8Array {
  const body = 3 + utf8.encode(key).length + value.length;
  let length = body + String(body).length;
  if (String(length).length > String(body).length) length += 1;
  return new Uint8Array([
    ...utf8.encode(`${length} ${key}=`),
    ...value,
    ...utf8.encode("\n"),
  ]);
}

const padding = (size: number): Uint8Array =>
  new Uint8Array((BLOCK - (size % BLOCK)) % BLOCK);

/** The headers of one entry: a pax header first when its name or link is too long. */
function headers(e: TreeEntry, owner: Owner): readonly Uint8Array[] {
  const name = utf8.encode(e.kind === "dir" ? `${e.path}/` : e.path);
  const link = utf8.encode(e.kind === "symlink" ? e.target : "");
  const records = [
    ...(name.length > FIELD ? [paxRecord("path", name)] : []),
    ...(link.length > FIELD ? [paxRecord("linkpath", link)] : []),
  ];
  const [type, mode, size] =
    e.kind === "file"
      ? ["0", e.mode, e.size]
      : e.kind === "dir"
        ? ["5", e.mode, 0]
        : ["2", 0o777, 0];
  const own = header(type, name, link, mode, size, owner);
  if (records.length === 0) return [own];
  const pax = new Uint8Array(records.flatMap((r) => [...r]));
  const paxName = utf8.encode("././@PaxHeader");
  const empty = new Uint8Array();
  return [
    header("x", paxName, empty, 0o644, pax.length, owner),
    pax,
    padding(pax.length),
    own,
  ];
}

/**
 * Writes the tree's canonical archive to `out`, one file's bytes at a time from `artifacts`. A
 * tree that breaks a tree rule is artifact_corrupt before any byte is written.
 * A missing or corrupt file artifact, or one whose size isn't the tree's, stops it.
 */
export async function buildTar(
  tree: Tree,
  artifacts: Pick<ArtifactStore, "get">,
  owner: Owner,
  out: (chunk: Uint8Array) => void,
): Promise<Result<void, LogError>> {
  if (
    ![owner.uid, owner.gid].every(
      (id) => Number.isInteger(id) && id >= 0 && id <= MAX_ID,
    )
  )
    throw new Error(`uid/gid outside 0..${MAX_ID}`);
  const why = broken(tree.entries);
  if (why !== undefined)
    return err(logError("artifact_corrupt", `the tree can't be built: ${why}`));
  for (const e of tree.entries) {
    for (const block of headers(e, owner)) out(block);
    if (e.kind !== "file") continue;
    const bytes = await artifacts.get(e.sha256);
    if (!bytes.ok) return bytes;
    if (bytes.value.length !== e.size)
      return err(
        logError(
          "artifact_corrupt",
          `artifact ${e.sha256} isn't ${e.size} bytes long`,
        ),
      );
    out(bytes.value);
    out(padding(e.size));
  }
  out(new Uint8Array(2 * BLOCK));
  return ok(undefined);
}

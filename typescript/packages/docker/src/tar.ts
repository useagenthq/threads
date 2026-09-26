import { DockerError, unavailable } from "./wire";

// The ustar archives the Engine's `/archive` endpoints move files in. Writing is the
// adapter's own, not core's tree builder: these are control files (root-owned 0700
// directories, a 0600 stdin stage) that a snapshot tree has no notion of, and the uid and gid
// in the header are load-bearing — `docker cp` from a Mac writes the host's uid and the exec
// then fails with EACCES. Reading is a trust boundary: whatever a container hands back is
// parsed strictly, and anything malformed is an unavailable DockerError.

const BLOCK = 512;
/** The largest member this adapter reads back: core's per-file archive cap. */
const MAX_FILE = 256 * 2 ** 20;

export type TarEntry = {
  readonly name: string;
  readonly mode: number;
  readonly uid: number;
  readonly gid: number;
  /** A directory when absent. */
  readonly bytes?: Uint8Array;
};

const utf8 = new TextEncoder();

function octal(block: Uint8Array, at: number, digits: number, value: number) {
  block.set(utf8.encode(`${value.toString(8).padStart(digits, "0")}\0`), at);
}

/** One ustar header block, with its checksum. */
function header(
  name: Uint8Array,
  type: string,
  mode: number,
  size: number,
  owner: { readonly uid: number; readonly gid: number },
): Uint8Array {
  const block = new Uint8Array(BLOCK);
  block.set(name.subarray(0, 100), 0);
  octal(block, 100, 7, mode & 0o7777);
  octal(block, 108, 7, owner.uid);
  octal(block, 116, 7, owner.gid);
  octal(block, 124, 11, size);
  octal(block, 136, 11, 0);
  block.set(utf8.encode(type), 156);
  block.set(utf8.encode("ustar\u000000"), 257);
  block.fill(0x20, 148, 156);
  const sum = block.reduce((n, b) => n + b, 0);
  block.set(utf8.encode(`${sum.toString(8).padStart(6, "0")}\0 `), 148);
  return block;
}

const padded = (size: number): Uint8Array =>
  new Uint8Array((BLOCK - (size % BLOCK)) % BLOCK);

/** A pax `path` record, for a name over the 100-byte header field. */
function paxPath(name: Uint8Array): readonly Uint8Array[] {
  const body = 3 + "path".length + name.length;
  let length = body + String(body).length;
  if (String(length).length > String(body).length) length += 1;
  const data = new Uint8Array([
    ...utf8.encode(`${length} path=`),
    ...name,
    ...utf8.encode("\n"),
  ]);
  return [
    header(utf8.encode("@PaxHeader"), "x", 0o644, data.length, {
      uid: 0,
      gid: 0,
    }),
    data,
    padded(data.length),
  ];
}

/** The archive of `entries`, each written with exactly the owner and mode given. */
export function tarOf(entries: readonly TarEntry[]): Uint8Array {
  const parts: Uint8Array[] = [];
  for (const e of entries) {
    const dir = e.bytes === undefined;
    const name = utf8.encode(dir ? `${e.name}/` : e.name);
    if (name.length > 100) parts.push(...paxPath(name));
    const size = e.bytes?.length ?? 0;
    parts.push(header(name, dir ? "5" : "0", e.mode, size, e));
    if (e.bytes !== undefined) parts.push(e.bytes, padded(size));
  }
  parts.push(new Uint8Array(2 * BLOCK));
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

export type TarFile = { readonly name: string; readonly bytes: Uint8Array };

const text = new TextDecoder();

/** A NUL-terminated header field. */
function field(block: Uint8Array, at: number, length: number): string {
  const raw = block.subarray(at, at + length);
  const end = raw.indexOf(0);
  return text.decode(end === -1 ? raw : raw.subarray(0, end)).trim();
}

function size(block: Uint8Array, offset: number): number {
  const value = field(block, 124, 12);
  const n = value === "" ? 0 : Number.parseInt(value, 8);
  if (!Number.isSafeInteger(n) || n < 0)
    throw unavailable(`the archive has a bad size at byte ${offset}`);
  if (n > MAX_FILE)
    throw unavailable(`the archive holds a member over ${MAX_FILE} bytes`);
  return n;
}

/** The `path` record of a pax header, when it has one. */
function paxName(data: Uint8Array): string | undefined {
  for (const record of text.decode(data).split("\n")) {
    const at = record.indexOf(" path=");
    if (at !== -1) return record.slice(at + " path=".length);
  }
  return undefined;
}

/**
 * The regular files of a tar archive, by name. Directories and every other member type are
 * skipped: this reads back what the adapter itself wrote and what the supervisor left in
 * `state/`, and nothing else is meaningful to it.
 */
export function untar(archive: Uint8Array): readonly TarFile[] {
  const files: TarFile[] = [];
  let override: string | undefined;
  for (let at = 0; at + BLOCK <= archive.length; ) {
    const block = archive.subarray(at, at + BLOCK);
    at += BLOCK;
    if (block.every((b) => b === 0)) break;
    const length = size(block, at);
    const body = archive.subarray(at, at + length);
    if (body.length < length)
      throw unavailable("the archive ends inside a member");
    at += length + padded(length).length;
    const type = field(block, 156, 1);
    if (type === "x" || type === "g") {
      override = paxName(body) ?? override;
      continue;
    }
    const name = override ?? field(block, 0, 100);
    override = undefined;
    if (type === "" || type === "0") files.push({ name, bytes: body.slice() });
  }
  return files;
}

/** The one file of a single-member archive (an archive GET of a file path). */
export function onlyFile(archive: Uint8Array, path: string): Uint8Array {
  const [only, ...more] = untar(archive);
  if (only === undefined || more.length > 0)
    throw new DockerError(
      "unavailable",
      `the archive of ${path} holds ${only === undefined ? "no" : "several"} files`,
    );
  return only.bytes;
}

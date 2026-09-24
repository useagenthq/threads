// One tar header block and one pax extended header, parsed strictly (spec/schema/README.md,
// "Snapshot manifest"). Only the fields the tree uses are read: name, mode, size, checksum,
// typeflag, linkname, magic and the ustar prefix.

export const BLOCK = 512;

/** A parsed header. `mode` is read for regular files and directories only (0 otherwise). */
export type Header = {
  readonly type: string;
  readonly name: Uint8Array;
  readonly link: Uint8Array;
  readonly size: number;
  readonly mode: number;
};

/** The pax records the tree uses; an empty value is the same as an absent one. */
export type Pax = {
  readonly path?: Uint8Array;
  readonly linkpath?: Uint8Array;
  readonly size?: number;
};

const OCTAL = /^[0-7]{1,12}$/;
const DECIMAL = /^[0-9]{1,15}$/;
const latin1 = new TextDecoder("latin1");

export function isZero(block: Uint8Array): boolean {
  return block.every((b) => b === 0);
}

/** A NUL-terminated field's bytes. */
function cstring(block: Uint8Array, at: number, length: number): Uint8Array {
  const field = block.subarray(at, at + length);
  const end = field.indexOf(0);
  return end === -1 ? field : field.subarray(0, end);
}

/** An octal number field: optional leading spaces, digits, then NULs or spaces. */
function octal(
  block: Uint8Array,
  at: number,
  length: number,
): number | undefined {
  const text = latin1
    .decode(block.subarray(at, at + length))
    .replace(/[\0 ]+$/, "")
    .replace(/^ +/, "");
  return OCTAL.test(text) ? Number.parseInt(text, 8) : undefined;
}

function checksum(block: Uint8Array): number {
  let sum = 0;
  for (let i = 0; i < BLOCK; i++)
    sum += i >= 148 && i < 156 ? 32 : (block[i] ?? 0);
  return sum;
}

function ascii(block: Uint8Array, at: number, length: number): string {
  return latin1.decode(block.subarray(at, at + length));
}

/** POSIX ustar (with the prefix field) or GNU (without); anything else is malformed. */
function format(block: Uint8Array): "posix" | "gnu" | undefined {
  if (ascii(block, 257, 8) === "ustar\u000000") return "posix";
  if (ascii(block, 257, 8) === "ustar  \0") return "gnu";
  return undefined;
}

/** The header block's fields; undefined when malformed or its checksum fails. */
export function parseHeader(block: Uint8Array): Header | undefined {
  const kind = format(block);
  const size = octal(block, 124, 12);
  if (kind === undefined || size === undefined) return undefined;
  if (octal(block, 148, 8) !== checksum(block)) return undefined;
  const type = ascii(block, 156, 1);
  const name = cstring(block, 0, 100);
  const prefix = kind === "posix" ? cstring(block, 345, 155) : new Uint8Array();
  // Links and pax headers carry no mode the tree keeps.
  const moded = type === "0" || type === "\0" || type === "5";
  const mode = moded ? octal(block, 100, 8) : 0;
  if (mode === undefined) return undefined;
  return {
    type,
    name:
      prefix.length === 0 ? name : new Uint8Array([...prefix, 0x2f, ...name]),
    link: cstring(block, 157, 100),
    size,
    mode: mode & 0o7777,
  };
}

const SPACE = 0x20;
const EQUALS = 0x3d;
const NEWLINE = 0x0a;

type Record = { readonly key: string; readonly value: Uint8Array };

/** `<length> <key>=<value>\n` records, the length counting the whole record. */
function records(data: Uint8Array): readonly Record[] | undefined {
  const out: Record[] = [];
  for (let at = 0; at < data.length; ) {
    const space = data.indexOf(SPACE, at);
    const digits = space === -1 ? "" : latin1.decode(data.subarray(at, space));
    if (!DECIMAL.test(digits)) return undefined;
    const end = at + Number(digits);
    if (end > data.length || end <= space + 1 || data[end - 1] !== NEWLINE)
      return undefined;
    const body = data.subarray(space + 1, end - 1);
    const equals = body.indexOf(EQUALS);
    if (equals < 1) return undefined;
    out.push({
      key: latin1.decode(body.subarray(0, equals)),
      value: body.subarray(equals + 1),
    });
    at = end;
  }
  return out;
}

/** A pax extended header's records; undefined when malformed or sparse (GNU.sparse.*). */
export function parsePax(data: Uint8Array): Pax | undefined {
  const found = records(data);
  if (found === undefined) return undefined;
  let pax: Pax = {};
  for (const { key, value } of found) {
    if (key.startsWith("GNU.sparse.")) return undefined;
    if (key !== "path" && key !== "linkpath" && key !== "size") continue;
    const { [key]: _, ...rest } = pax;
    if (value.length === 0) pax = rest;
    else if (key !== "size") pax = { ...rest, [key]: value };
    else if (DECIMAL.test(latin1.decode(value)))
      pax = { ...rest, size: Number(latin1.decode(value)) };
    else return undefined;
  }
  return pax;
}

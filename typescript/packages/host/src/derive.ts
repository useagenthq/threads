import { sha256Hex, ThreadId } from "@threads/core/host";

// Derived ids: a name that is a function of its fields, with no lookup table and nothing
// recorded. One implementation, because the UI's chat key (spec/schema/ui/README.md) and A2A's
// context (spec/schema/a2a/) derive the same way under different domain strings, and a byte of
// difference between them would let one surface reach the other's thread.

/** 4-byte big-endian UTF-8 length, then the bytes: no two field splits collide. */
function lp(text: string): Uint8Array {
  const bytes = new TextEncoder().encode(text);
  const out = new Uint8Array(4 + bytes.length);
  new DataView(out.buffer).setUint32(0, bytes.length);
  out.set(bytes, 4);
  return out;
}

function joined(parts: readonly Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

/**
 * A sha256 digest's first 16 bytes as a UUIDv8: version 8 and the RFC 4122 variant written over
 * the hex, so the result is a valid UUID that no version-7 generator can produce.
 */
function uuidv8(digestHex: string): string {
  const hex = digestHex.slice(0, 32);
  const version = `8${hex.slice(13, 16)}`;
  const variant = (
    (Number.parseInt(hex.slice(16, 17), 16) & 0x3) |
    0x8
  ).toString(16);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${version}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

/**
 * `UUIDv8 of sha256(lp(domain) ‖ lp(field) ‖ …)`. The domain string is what keeps two surfaces'
 * derivations apart: the same fields under another domain name a different id.
 */
export function derivedId(domain: string, fields: readonly string[]): string {
  return uuidv8(sha256Hex(joined([domain, ...fields].map(lp))));
}

/** `derivedId` as a thread id: what a chat key and an A2A context each name. */
export function derivedThreadId(
  domain: string,
  fields: readonly string[],
): ThreadId {
  return ThreadId.parse(derivedId(domain, fields));
}

/** `UUIDv8 of sha256(domain ‖ field ‖ …)`, with no length prefix: for fixed-width fields only. */
export function derivedUuid(domain: string, fields: readonly string[]): string {
  return uuidv8(
    sha256Hex(new TextEncoder().encode([domain, ...fields].join(""))),
  );
}

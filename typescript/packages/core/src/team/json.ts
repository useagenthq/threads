import { canonicalLine } from "../store/encode";

/**
 * The RFC 8785 bytes of a value parsed from a log line: what the team tables' JSON columns hold
 * (store.sql). A parsed event always serializes, so a failure is a bug, never an input error.
 */
export function jsonBytes(value: unknown): Uint8Array {
  const bytes = canonicalLine(value);
  if (!bytes.ok) throw new Error(`not JSON: ${bytes.error.message}`);
  return bytes.value;
}

/** Two parsed values are the same JSON, compared as their canonical bytes. */
export function sameJson(a: unknown, b: unknown): boolean {
  const x = jsonBytes(a);
  const y = jsonBytes(b);
  return x.length === y.length && x.every((byte, i) => byte === y[i]);
}

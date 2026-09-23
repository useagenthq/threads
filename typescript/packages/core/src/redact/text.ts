import { holds } from "./registry";
import { holdsAny, replacements, scan, streamOf } from "./scan";

// Text redaction: every string the writer records and every text artifact the host stores.

const same = (text: string): string => text;

/**
 * `text` with every registered value replaced by its marker. If the result still holds a value
 * (a marker joined to its neighbours), the whole string becomes `[redacted]`.
 */
export function redactSecrets(text: string): string {
  const r = replacements(same);
  const { out } = scan(text, r, true);
  return holdsAny(out, r) ? r.redacted : out;
}

/**
 * `value` with every string in it redacted, object keys included: structured data about to be
 * recorded. A redacted key that meets another is numbered; a number that would hold a value
 * is skipped, so no entry is lost and no key holds a value.
 */
export function redactStrings(value: unknown): unknown {
  if (typeof value === "string") return redactSecrets(value);
  if (Array.isArray(value)) return value.map(redactStrings);
  if (typeof value !== "object" || value === null) return value;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    const base = redactSecrets(k);
    let name = base;
    for (let n = 2; Object.hasOwn(out, name) || holds(name); n += 1)
      name = `${base} (${n})`;
    out[name] = redactStrings(v);
  }
  return out;
}

/** A text stream (model deltas) redacted as it arrives, a value split across chunks included. */
export function redactStream(): {
  readonly feed: (text: string) => string;
  readonly end: () => string;
} {
  return streamOf(same);
}

import { ordered } from "./registry";

// The matcher both text and bytes redaction run: the longest registered value at each position,
// then the whole output checked again, so a marker joined to its neighbours never forms a value.

/** Value → marker, in the unit `encode` gives (text, or bytes as latin1 text). */
export type Replacements = {
  readonly markers: ReadonlyMap<string, string>;
  /** What a piece of output that still holds a value becomes. */
  readonly redacted: string;
};

export function replacements(encode: (text: string) => string): Replacements {
  const markers = new Map(
    ordered().map(([v, l]) => [encode(v), encode(marker(l))]),
  );
  const values = [...markers.keys()];
  const plain = encode("[redacted]");
  return {
    markers,
    redacted: values.some((v) => plain.includes(v)) ? "" : plain,
  };
}

/** `[secret <label>]`, or `[secret]` when the label holds a registered value. */
function marker(label: string): string {
  const labelled = `[secret ${label}]`;
  return ordered().some(([v]) => labelled.includes(v)) ? "[secret]" : labelled;
}

/** Whether `text` holds any of `values` (already in the scan's unit). */
export function holdsAny(text: string, r: Replacements): boolean {
  return [...r.markers.keys()].some((v) => text.includes(v));
}

const escapeRegExp = (text: string): string =>
  text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * The longest value at each position, scanning left to right. Unless `final`, it stops where
 * the rest could still grow into a longer value (`abc` before a possible `abc123`), and returns
 * that rest to hold for the next chunk.
 */
export function scan(
  pending: string,
  r: Replacements,
  final: boolean,
): { readonly out: string; readonly rest: string } {
  const values = r.markers;
  if (values.size === 0) return { out: pending, rest: "" };
  const hold = final ? pending.length : heldFrom(pending, values);
  const pattern = new RegExp(
    [...values.keys()].map(escapeRegExp).join("|"),
    "g",
  );
  let out = "";
  let at = 0;
  for (const m of pending.matchAll(pattern)) {
    if (m.index >= hold) break;
    out += pending.slice(at, m.index) + (values.get(m[0]) ?? "");
    at = m.index + m[0].length;
  }
  const cut = Math.max(hold, at);
  return { out: out + pending.slice(at, cut), rest: pending.slice(cut) };
}

/** The first position whose rest is a proper prefix of a value: it may still become one. */
function heldFrom(
  pending: string,
  values: ReadonlyMap<string, string>,
): number {
  const longest = Math.max(...[...values.keys()].map((v) => v.length));
  for (
    let i = Math.max(0, pending.length - longest + 1);
    i < pending.length;
    i += 1
  ) {
    const tail = pending.slice(i);
    if (
      [...values.keys()].some(
        (v) => v.length > tail.length && v.startsWith(tail),
      )
    )
      return i;
  }
  return pending.length;
}

/**
 * A stream redacted as it arrives. A chunk that, joined to the tail already emitted, would hold
 * a value is emitted as the plain marker instead.
 */
export function streamOf(encode: (text: string) => string): {
  readonly feed: (text: string) => string;
  readonly end: () => string;
} {
  let pending = "";
  let emitted = "";
  const take = (final: boolean): string => {
    const r = replacements(encode);
    const { out, rest } = scan(pending, r, final);
    pending = rest;
    const shown = holdsAny(emitted + out, r) ? r.redacted : out;
    // Only the last longest-1 units can join the next chunk into a value.
    const keep = Math.max(0, ...[...r.markers.keys()].map((v) => v.length - 1));
    emitted = keep === 0 ? "" : (emitted + shown).slice(-keep);
    return shown;
  };
  return {
    feed: (text) => {
      pending += text;
      return take(false);
    },
    end: () => take(true),
  };
}

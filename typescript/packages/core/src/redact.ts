import { Buffer } from "node:buffer";
import type { ArtifactSink } from "./store/artifacts";

// Secret redaction (spec/schema/README.md "Secret redaction", C5). Every credential value the
// host resolves is registered here; nothing is recorded with one in it. Event data is redacted
// where the writer stores it, so every event (results, model output, hook text, input) passes
// one boundary; the bytes stored beside events (spilled output, summaries, commits) are
// redacted where they are written, streams included.

/** Values resolved in this host process, each with the smallest label it was registered under. */
const registered = new Map<string, string>();

/** Compares by Unicode code point, as Python compares strings, so both languages agree. */
function byCodePoints(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i += 1) {
    const d = (x[i]?.codePointAt(0) ?? 0) - (y[i]?.codePointAt(0) ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

/** Registers a resolved value; recorded text shows `[secret <label>]` instead. */
export function register(value: string, label: string): void {
  const known = registered.get(value);
  if (known === undefined || byCodePoints(label, known) < 0)
    registered.set(value, label);
}

/** Value → replacement, longest value first, then by code point. */
function replacements(
  encode: (text: string) => string,
): ReadonlyMap<string, string> {
  const ordered = [...registered].toSorted(
    ([a], [b]) =>
      Array.from(b).length - Array.from(a).length || byCodePoints(a, b),
  );
  return new Map(ordered.map(([v, l]) => [encode(v), encode(`[secret ${l}]`)]));
}

const escapeRegExp = (text: string): string =>
  text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * The longest value at each position, scanning left to right. Unless `final`, it stops where
 * the rest could still grow into a longer value (`abc` before a possible `abc123`), and returns
 * that rest to hold for the next chunk.
 */
function scan(
  pending: string,
  values: ReadonlyMap<string, string>,
  final: boolean,
): { readonly out: string; readonly rest: string } {
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

const same = (text: string): string => text;

/** `text` with every resolved value replaced by its label. */
export function redactSecrets(text: string): string {
  return scan(text, replacements(same), true).out;
}

/** `value` with every string in it redacted: structured data about to be recorded. */
export function redactStrings(value: unknown): unknown {
  if (typeof value === "string") return redactSecrets(value);
  if (Array.isArray(value)) return value.map(redactStrings);
  if (typeof value === "object" && value !== null)
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [k, redactStrings(v)]),
    );
  return value;
}

/** A text stream (model deltas) redacted as it arrives, a value split across chunks included. */
export function redactStream(): {
  readonly feed: (text: string) => string;
  readonly end: () => string;
} {
  let pending = "";
  const take = (final: boolean): string => {
    const { out, rest } = scan(pending, replacements(same), final);
    pending = rest;
    return out;
  };
  return {
    feed: (text) => {
      pending += text;
      return take(false);
    },
    end: () => take(true),
  };
}

// Bytes are scanned as latin1 text, one char per byte, so a value split inside a multi-byte
// character is held and matched like any other.
const bytes = (text: string): string =>
  Buffer.from(text, "utf8").toString("latin1");

/** An artifact sink (a spilled exec output) that stores its bytes redacted as they stream in. */
export function redactingSink(sink: ArtifactSink): ArtifactSink {
  let pending = "";
  const take = (final: boolean): void => {
    const { out, rest } = scan(pending, replacements(bytes), final);
    pending = rest;
    sink.write(Buffer.from(out, "latin1"));
  };
  return {
    write: (chunk) => {
      pending += Buffer.from(chunk).toString("latin1");
      take(false);
    },
    finish: () => {
      take(true);
      return sink.finish();
    },
    abort: sink.abort,
  };
}

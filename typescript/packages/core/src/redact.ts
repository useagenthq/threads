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

/** Forgets every registered value: each test starts with none (test/setup.ts). */
export function forgetSecrets(): void {
  registered.clear();
}

/**
 * The marker for a value: its label, unless some registered value appears in it (`api` in
 * `[secret fake.apiKey]`); then the first plainer marker that holds none. A marker never
 * brings a value back.
 */
function marker(label: string): string {
  const candidates = [`[secret ${label}]`, "[secret]", "[redacted]"];
  return (
    candidates.find(
      (m) => ![...registered.keys()].some((v) => m.includes(v)),
    ) ?? ""
  );
}

/** Value → replacement, longest value first, then by code point. */
function replacements(
  encode: (text: string) => string,
): ReadonlyMap<string, string> {
  const ordered = [...registered].toSorted(
    ([a], [b]) =>
      Array.from(b).length - Array.from(a).length || byCodePoints(a, b),
  );
  return new Map(ordered.map(([v, l]) => [encode(v), encode(marker(l))]));
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

/**
 * `value` with every string in it redacted, object keys included: structured data about to be
 * recorded. A redacted key that meets another is numbered, so no entry is lost.
 */
export function redactStrings(value: unknown): unknown {
  if (typeof value === "string") return redactSecrets(value);
  if (Array.isArray(value)) return value.map(redactStrings);
  if (typeof value !== "object" || value === null) return value;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    const base = redactSecrets(k);
    let name = base;
    for (let n = 2; Object.hasOwn(out, name); n += 1) name = `${base} (${n})`;
    out[name] = redactStrings(v);
  }
  return out;
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

/** `data` with every resolved value replaced (a textual artifact about to be stored). */
export function redactBytes(data: Uint8Array): Uint8Array {
  const text = Buffer.from(data).toString("latin1");
  return Buffer.from(scan(text, replacements(bytes), true).out, "latin1");
}

/** Whether `data` holds a resolved value. */
export function holdsSecret(data: Uint8Array): boolean {
  return !Buffer.from(data).equals(redactBytes(data));
}

/**
 * Provider continuation material (signed or encrypted reasoning, a hosted tool's item) is
 * replayed byte-exact, so it is never edited: one that holds a resolved value is refused.
 */
export class SecretInProviderOutput extends Error {
  constructor() {
    super("a registered secret appeared in unmodifiable provider output");
  }
}

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

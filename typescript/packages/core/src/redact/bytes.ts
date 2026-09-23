import { Buffer } from "node:buffer";
import type { ArtifactSink } from "../store/artifacts";
import { generation, ordered } from "./registry";
import { holdsAny, replacements, scan, streamOf } from "./scan";

// Bytes the host stores: redacted when they are text it may edit (a spilled exec output, a
// fetched page), refused when they must stay byte-exact (provider material, a screenshot, a
// knowledge source, an import).

// Bytes are scanned as latin1 text, one char per byte, so a value split inside a multi-byte
// character is held and matched like any other.
const latin1 = (text: string): string =>
  Buffer.from(text, "utf8").toString("latin1");

/** A value's forms in stored bytes: as is, JSON-escaped, and JSON-escaped to ASCII. */
function forms(value: string): readonly Buffer[] {
  const json = JSON.stringify(value).slice(1, -1);
  const ascii = json.replace(
    /[\u0080-￿]/g,
    (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  return [...new Set([value, json, ascii])].map((f) => Buffer.from(f, "utf8"));
}

/**
 * Whether `data` holds a registered value in any form a store or a later parse could give
 * back: raw UTF-8, or JSON-escaped (quotes, backslashes, controls, `\uXXXX`).
 */
export function containsSecret(data: Uint8Array): boolean {
  const bytes = Buffer.from(data);
  return ordered().some(([v]) => forms(v).some((f) => bytes.includes(f)));
}

/**
 * `data` (a textual artifact) with every registered value replaced. Bytes that still hold one,
 * or its JSON-escaped form, are the caller's to refuse (`containsSecret`).
 */
export function redactBytes(data: Uint8Array): Uint8Array {
  const r = replacements(latin1);
  const { out } = scan(Buffer.from(data).toString("latin1"), r, true);
  return Buffer.from(holdsAny(out, r) ? r.redacted : out, "latin1");
}

/**
 * Provider continuation material (signed or encrypted reasoning, a hosted tool's item) is
 * replayed byte-exact, so it is never edited: one that holds a registered value is refused.
 */
export class SecretInProviderOutput extends Error {
  constructor() {
    super("a registered secret appeared in unmodifiable provider output");
  }
}

/** A spilled exec output's sink; `finish` gives undefined when the spill was dropped. */
export type RedactingSink = Omit<ArtifactSink, "finish"> & {
  readonly finish: () => ReturnType<ArtifactSink["finish"]> | undefined;
};

/**
 * An artifact sink (a spilled exec output) that stores its bytes redacted as they stream in.
 * Bytes already written can't be revisited, so a value registered while it streams drops it.
 */
export function redactingSink(sink: ArtifactSink): RedactingSink {
  const stream = streamOf(latin1);
  const since = generation();
  const write = (text: string): void => {
    sink.write(Buffer.from(text, "latin1"));
  };
  return {
    write: (chunk) => write(stream.feed(Buffer.from(chunk).toString("latin1"))),
    finish: () => {
      write(stream.end());
      if (generation() === since) return sink.finish();
      sink.abort();
      return undefined;
    },
    abort: sink.abort,
  };
}

import { z } from "zod";
import { ConfigError } from "../agent/errors";
import { ErrorCode, LogLine } from "../log";

// The registered values: every credential the host resolves (spec/schema/README.md "Secret
// redaction", C5). A value is at least 8 characters and never a literal of the event schema,
// so the line envelope and the schema's keys can stay unredacted: none can hold one.

const MIN_CHARS = 8;

/** Values resolved in this host process, each with the smallest label it was registered under. */
const registered = new Map<string, string>();

/** Compares by Unicode code point, as Python compares strings, so both languages agree. */
export function byCodePoints(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i += 1) {
    const d = (x[i]?.codePointAt(0) ?? 0) - (y[i]?.codePointAt(0) ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

let literals: ReadonlySet<string> | undefined;

/** Every event type, enum value and field name of the event schema, read from its Zod source. */
function schemaLiterals(): ReadonlySet<string> {
  literals ??= new Set(
    [z.toJSONSchema(LogLine), z.toJSONSchema(ErrorCode)].flatMap(collect),
  );
  return literals;
}

function collect(node: unknown): string[] {
  if (Array.isArray(node)) return node.flatMap(collect);
  if (typeof node !== "object" || node === null) return [];
  return Object.entries(node).flatMap(([key, value]) => [
    ...(key === "properties" && typeof value === "object" && value !== null
      ? Object.keys(value)
      : []),
    ...(key === "enum" && Array.isArray(value)
      ? value.filter((v) => typeof v === "string")
      : []),
    ...(key === "const" && typeof value === "string" ? [value] : []),
    ...collect(value),
  ]);
}

/**
 * Registers a resolved value; recorded text shows `[secret <label>]` instead. A value shorter
 * than 8 characters, or equal to a schema literal, is refused (ConfigError invalid_config).
 */
export function register(value: string, label: string): void {
  if (Array.from(value).length < MIN_CHARS)
    throw new ConfigError(
      "invalid_config",
      "secret values must be at least 8 characters",
    );
  if (schemaLiterals().has(value))
    throw new ConfigError(
      "invalid_config",
      "a secret value can't be a literal of the event schema",
    );
  const known = registered.get(value);
  if (known === undefined || byCodePoints(label, known) < 0)
    registered.set(value, label);
}

/** Forgets every registered value: each test starts with none (test/setup.ts). */
export function forgetSecrets(): void {
  registered.clear();
}

/** Value and label, longest value first, then by code point. */
export function ordered(): readonly (readonly [string, string])[] {
  return [...registered].toSorted(
    ([a], [b]) =>
      Array.from(b).length - Array.from(a).length || byCodePoints(a, b),
  );
}

/** Whether `text` holds a registered value. */
export function holds(text: string): boolean {
  return [...registered.keys()].some((v) => text.includes(v));
}

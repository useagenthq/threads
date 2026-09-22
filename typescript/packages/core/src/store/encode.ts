import { canonicalize, JsonValue } from "../log";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";

const utf8 = new TextEncoder();

/** The RFC 8785 line for a value this writer built; a non-JSON value is an invalid line. */
export function canonicalLine(value: unknown): Result<Uint8Array, LogError> {
  const json = JsonValue.safeParse(value);
  const text = json.success ? canonicalize(json.data) : undefined;
  return text?.ok === true
    ? ok(utf8.encode(text.value))
    : err(logError("invalid_line", "the value is not JSON"));
}

/** A lowercase UUIDv7 at `ms` (wire rule 5: writers generate v7). */
export function uuidv7(ms: number): string {
  const b = crypto.getRandomValues(new Uint8Array(16));
  let t = ms;
  for (let i = 5; i >= 0; i -= 1) {
    b[i] = t % 256;
    t = Math.floor(t / 256);
  }
  b[6] = ((b[6] ?? 0) & 0x0f) | 0x70;
  b[8] = ((b[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

import type { ArtifactRef } from "../log";
import { contextPolicy } from "./policy";
import type { Session } from "./session";

// a tool result is recorded as text the host may show (secrets redacted, C5), and
// text over spill.threshold_bytes keeps its full bytes as `ref` behind a bounded preview.

const encoder = new TextEncoder();
const decoder = new TextDecoder();

export type Recorded = {
  /** The whole result as recorded (redacted). */
  readonly text: string;
  readonly preview: string;
  /** Present exactly when the result was spilled. */
  readonly ref?: ArtifactRef;
};

/** The largest end <= `at` that doesn't split a UTF-8 sequence. */
function boundary(bytes: Uint8Array, at: number): number {
  let end = Math.min(at, bytes.length);
  while (end > 0 && end < bytes.length && ((bytes[end] ?? 0) & 0xc0) === 0x80)
    end -= 1;
  return end;
}

export function recordOutput(
  s: Session,
  callId: string,
  output: string,
): Recorded {
  const text = s.config.redact?.(output) ?? output;
  const bytes = encoder.encode(text);
  const spill = contextPolicy(s.fold.policy).spill;
  if (bytes.length <= spill.threshold_bytes) return { text, preview: text };
  // ponytail: request_budget_bytes re-spill across one batch is not applied yet.
  const head = bytes.subarray(0, boundary(bytes, spill.head_bytes));
  const tail = bytes.subarray(
    boundary(bytes, Math.max(head.length, bytes.length - spill.tail_bytes)),
  );
  const marker = `\n[output truncated: ${bytes.length} bytes; read_tool_result(call_id="${callId}", offset, length) returns the rest]\n`;
  return {
    text,
    preview: `${decoder.decode(head)}${marker}${decoder.decode(tail)}`,
    ref: s.store(bytes, "text/plain"),
  };
}

import type { KnownEvent } from "../log";
import { ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import {
  edits,
  eventLine,
  type LineContext,
  type ReadRef,
  summaryLine,
  userLine,
} from "./lines";
import { jcs, line0 } from "./prefix";
import { view, walk } from "./view";

/** A Render v1 request: all its bytes, and line 0 (the declared prefix) with its `\n`. */
export type Rendered = {
  readonly bytes: Uint8Array;
  readonly prefix: Uint8Array;
};

const COMPACT_INSTRUCTION =
  "Summarize the conversation so far for your own continuation. Keep the user's goals and " +
  "constraints, decisions made, files and identifiers touched, open tasks with their status, " +
  "and the next step. Reply with the summary only.";

const encoder = new TextEncoder();

/**
 * Render v1 of the events before a request (spec/schema/README.md; reference:
 * spec/tools/fixtures/render.py). `instruction` is the compaction side request's last line.
 * Every artifact a rendered line needs is read and verified; a missing or corrupt one fails.
 */
export function render(
  events: readonly KnownEvent[],
  read: ReadRef,
  instruction?: string,
): Result<Rendered, LogError> {
  const v = view(events);
  const ctx: LineContext = { view: v, edits: edits(events), read };
  const head = line0(events);
  const lines = [head];
  for (const entry of walk(v, events)) {
    const line =
      entry.kind === "summary"
        ? summaryLine(ctx, entry.compacted)
        : eventLine(ctx, entry.event);
    if (!line.ok) return line;
    if (line.value !== undefined) lines.push(jcs(line.value));
  }
  if (instruction !== undefined) lines.push(jcs(userLine(instruction)));
  return ok({
    bytes: encoder.encode(lines.map((line) => `${line}\n`).join("")),
    prefix: encoder.encode(`${head}\n`),
  });
}

/**
 * The compaction request's instruction: the fixed text, then the reasons of `before_compact`
 * guide decisions since the previous `model_request`. A guide without a reason adds nothing.
 */
export function compactionInstruction(events: readonly KnownEvent[]): string {
  const since = events.findLastIndex((e) => e.type === "model_request");
  const guides = events
    .slice(since + 1)
    .flatMap((e) =>
      e.type === "hook_decision" &&
      e.data.hook === "before_compact" &&
      e.data.decision === "guide" &&
      e.data.reason !== undefined
        ? [e.data.reason]
        : [],
    );
  return guides.length === 0
    ? COMPACT_INSTRUCTION
    : `${COMPACT_INSTRUCTION}\n\nAdditional instructions:\n${guides.join("\n")}`;
}

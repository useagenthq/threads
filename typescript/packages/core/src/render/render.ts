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

/**
 * A compaction side request's own parts: its instruction line, and for a requested compaction
 * the seq its history ends at (the request's).
 */
export type Side = { readonly instruction: string; readonly through?: number };

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
 * spec/tools/fixtures/render.py). `side` makes it a compaction side request (compactionSide).
 * Every artifact a rendered line needs is read and verified; a missing or corrupt one fails.
 */
export function render(
  events: readonly KnownEvent[],
  read: ReadRef,
  side?: Side,
): Result<Rendered, LogError> {
  const v = view(events);
  const ctx: LineContext = { view: v, edits: edits(events), read };
  const head = line0(events);
  const lines = [head];
  const through = side?.through ?? Number.POSITIVE_INFINITY;
  // A requested compaction's history ends at its request, each line rendered as it is now.
  const history = events.filter((e) => e.seq <= through);
  for (const entry of walk(v, history)) {
    const line =
      entry.kind === "summary"
        ? summaryLine(ctx, entry.compacted)
        : eventLine(ctx, entry.event);
    if (!line.ok) return line;
    if (line.value !== undefined) lines.push(jcs(line.value));
  }
  if (side !== undefined) lines.push(jcs(userLine(side.instruction)));
  return ok({
    bytes: encoder.encode(lines.map((line) => `${line}\n`).join("")),
    prefix: encoder.encode(`${head}\n`),
  });
}

/**
 * A compaction side request's parts (spec/schema/README.md, Render v1). Without a cause: the
 * fixed instruction, then the `before_compact` guides since the previous `model_request`. With
 * the compaction_requested it names: the history ends at that request, and the instruction
 * adds the request's instructions, then the guides appended after it. A guide without a reason
 * adds nothing.
 */
export function compactionSide(
  events: readonly KnownEvent[],
  cause?: string,
): Side {
  const request = events.find((e) => e.event_id === cause);
  if (request?.type !== "compaction_requested") {
    const since = events.findLastIndex((e) => e.type === "model_request");
    return { instruction: instruction(guides(events.slice(since + 1))) };
  }
  const asked = request.data.instructions;
  const after = guides(events.filter((e) => e.seq > request.seq));
  return {
    instruction: instruction([
      ...(asked === undefined ? [] : [asked]),
      ...after,
    ]),
    through: request.seq,
  };
}

function guides(events: readonly KnownEvent[]): readonly string[] {
  return events.flatMap((e) =>
    e.type === "hook_decision" &&
    e.data.hook === "before_compact" &&
    e.data.decision === "guide" &&
    e.data.reason !== undefined
      ? [e.data.reason]
      : [],
  );
}

function instruction(extra: readonly string[]): string {
  return extra.length === 0
    ? COMPACT_INSTRUCTION
    : `${COMPACT_INSTRUCTION}\n\nAdditional instructions:\n${extra.join("\n")}`;
}

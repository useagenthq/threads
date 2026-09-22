import { assertNever } from "../assert-never";
import type { EventOf, ResultEvent } from "../fold/state";
import type { ArtifactRef, ContentPart, Json, KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import { toolLine } from "./prefix";
import {
  assistantParts,
  type Compacted,
  type View,
  type VisibleEvent,
} from "./view";

/** Reads one artifact's verified bytes; errors carry the seq of the event that names it. */
export type ReadRef = (
  ref: ArtifactRef,
  seq: number,
) => Result<Uint8Array, LogError>;

/** context_edited before the request: cleared results, and redaction spans per (call, part). */
export type Edits = {
  readonly cleared: ReadonlySet<string>;
  readonly spans: ReadonlyMap<
    string,
    readonly { start: number; end: number }[]
  >;
};

export type LineContext = {
  readonly view: View;
  readonly edits: Edits;
  readonly read: ReadRef;
};

type Line = Result<Json | undefined, LogError>;

const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const encoder = new TextEncoder();
const REDACTED = encoder.encode("[redacted]");

/** The Render v1 framing escape: stored text can never close or forge a wrapper tag. */
export function esc(s: string): string {
  return s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function userLine(text: string): Json {
  return { role: "user", content: [{ type: "text", text }] };
}

function reference(source: string, id: string, body: string): string {
  return `<reference source="${esc(source)}" id="${esc(id)}" untrusted="true">\n${esc(body)}\n</reference>`;
}

const spanKey = (callId: string, part: number): string => `${callId}#${part}`;

export function edits(events: readonly KnownEvent[]): Edits {
  const cleared = new Set<string>();
  const spans = new Map<string, { start: number; end: number }[]>();
  for (const e of events) {
    if (e.type !== "context_edited") continue;
    for (const edit of e.data.edits) {
      if (edit.action === "clear") cleared.add(edit.call_id);
      else {
        const key = spanKey(edit.call_id, edit.part);
        spans.set(key, [...(spans.get(key) ?? []), ...edit.spans]);
      }
    }
  }
  return { cleared, spans };
}

/** An artifact's verified bytes as UTF-8 text; bytes that are not UTF-8 are corrupt as text. */
export function readText(
  read: ReadRef,
  ref: ArtifactRef,
  seq: number,
): Result<string, LogError> {
  const bytes = read(ref, seq);
  if (!bytes.ok) return bytes;
  try {
    return ok(utf8.decode(bytes.value));
  } catch {
    return err({
      code: "artifact_corrupt",
      message: `artifact ${ref.sha256} is not UTF-8`,
      seq,
    });
  }
}

/** Every artifact a rendered part references must exist and verify before dispatch. */
function checkParts(
  read: ReadRef,
  parts: readonly ContentPart[],
  seq: number,
): Result<void, LogError> {
  for (const part of parts) {
    if (
      part.type === "text" ||
      part.type === "tool_use" ||
      part.ref === undefined
    )
      continue;
    const bytes = read(part.ref, seq);
    if (!bytes.ok) return bytes;
  }
  return ok(undefined);
}

function withParts(
  ctx: LineContext,
  e: KnownEvent,
  parts: readonly ContentPart[],
  line: Json,
): Line {
  const checked = checkParts(ctx.read, parts, e.seq);
  return checked.ok ? ok(line) : checked;
}

/** The line one visible event renders, or undefined when it renders nothing. */
export function eventLine(ctx: LineContext, e: VisibleEvent): Line {
  switch (e.type) {
    case "user_input":
    case "steer":
      if (ctx.view.denied.has(e.event_id)) return ok(undefined);
      return "content" in e.data
        ? withParts(ctx, e, e.data.content, {
            role: "user",
            content: e.data.content,
          })
        : ok(userLine(e.data.text));
    case "injected":
      return injected(ctx, e);
    case "heartbeat":
      return ok(
        userLine(
          `<heartbeat>\nrunning: ${e.data.running_call_ids.map(esc).join(", ")}\n</heartbeat>`,
        ),
      );
    case "model_response":
    case "model_response_recovered": {
      const parts = assistantParts(ctx.view, e);
      return parts.length === 0
        ? ok(undefined)
        : withParts(ctx, e, parts, { role: "assistant", content: parts });
    }
    case "tools_changed":
      return ok({ role: "tools", tools: e.data.tools.map(toolLine) });
    case "tool_result":
    case "tool_result_late":
      return result(ctx, e);
    default:
      return assertNever(e);
  }
}

function injected(ctx: LineContext, e: EventOf<"injected">): Line {
  const { data } = e;
  const body =
    "text" in data ? ok(data.text) : readText(ctx.read, data.ref, e.seq);
  if (!body.ok) return body;
  const id = data.origin.id;
  const text =
    data.trust === "untrusted_reference"
      ? reference(data.source, id, body.value)
      : `<context source="${esc(data.source)}" id="${esc(id)}">\n${esc(body.value)}\n</context>`;
  return ok(userLine(text));
}

function result(ctx: LineContext, e: ResultEvent): Line {
  const { call_id: callId, is_error: isError } = e.data;
  const late = e.type === "tool_result_late" ? { late: true } : {};
  const line = (content: Json): Json => ({
    role: "tool",
    call_id: callId,
    is_error: isError,
    content,
    ...late,
  });
  if (ctx.edits.cleared.has(callId))
    return ok(
      line([
        {
          type: "text",
          text: `[tool result cleared: call_id=${callId}; read it with read_tool_result]`,
        },
      ]),
    );
  const raw = e.data.content ?? [{ type: "text", text: e.data.preview }];
  const parts = raw.map((part, i) => {
    const spans = ctx.edits.spans.get(spanKey(callId, i));
    return spans === undefined || part.type !== "text"
      ? part
      : { type: "text" as const, text: redact(part.text, spans) };
  });
  return withParts(ctx, e, parts, line(parts));
}

/** Replaces each UTF-8 byte span with `[redacted]`, right to left so offsets stay valid. */
function redact(
  text: string,
  spans: readonly { start: number; end: number }[],
): string {
  let bytes = encoder.encode(text);
  for (const { start, end } of spans.toSorted((a, b) => b.start - a.start)) {
    bytes = Uint8Array.from([
      ...bytes.subarray(0, start),
      ...REDACTED,
      ...bytes.subarray(end),
    ]);
  }
  return utf8.decode(bytes);
}

export function summaryLine(ctx: LineContext, c: Compacted): Line {
  const ref = c.data.summary_ref;
  const body = readText(ctx.read, ref, c.seq);
  return body.ok
    ? ok(userLine(reference("summary", ref.sha256, body.value)))
    : body;
}

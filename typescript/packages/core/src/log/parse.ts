import type { z } from "zod";
import { err, ok, type Result } from "../result";
import { Head, Header } from "./envelope";
import type { ErrorCode } from "./errors";
import { KnownEvent } from "./events";
import { canonicalize } from "./jcs";
import { parseStrictJson } from "./json";
import { UnknownEvent } from "./line";

/** The maximum line; anything larger goes in an artifact (wire rule 2). */
export const MAX_LINE_BYTES: number = 1024 * 1024;

export type ParseError = {
  readonly code: Extract<
    ErrorCode,
    "invalid_line" | "unsupported_format" | "unsupported_critical_event"
  >;
  readonly message: string;
  /** The line's `seq`, when it has a readable one. */
  readonly seq?: number;
};

/** A parsed line, tagged by what it is. Unknown non-critical events are kept: reduce skips them. */
export type ParsedLine =
  | { readonly kind: "header"; readonly header: Header }
  | { readonly kind: "head"; readonly head: Head }
  | { readonly kind: "event"; readonly event: KnownEvent }
  | { readonly kind: "unknown_event"; readonly event: UnknownEvent };

const FORMATS = new Set(["threads.log", "threads.head"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function describe(error: z.ZodError): string {
  const issue = error.issues[0];
  return issue === undefined
    ? "invalid"
    : `${issue.path.join(".") || "(root)"}: ${issue.message}`;
}

function invalid(
  message: string,
  seq?: number,
): { readonly ok: false; readonly error: ParseError } {
  return err(
    seq === undefined
      ? { code: "invalid_line", message }
      : { code: "invalid_line", message, seq },
  );
}

function readSeq(value: Record<string, unknown>): number | undefined {
  const { seq } = value;
  return typeof seq === "number" ? seq : undefined;
}

// Wire rule 8: a known format at a newer integer version is a newer writer, not corruption.
// Any other bad version or format is invalid. The error seq is 0 for a header, the head's own seq.
function parseFramingLine(
  value: Record<string, unknown>,
): Result<ParsedLine, ParseError> {
  const { format, format_version: version } = value;
  const isHead = format === "threads.head";
  const seq = isHead ? readSeq(value) : 0;
  const newer =
    typeof version === "number" && Number.isInteger(version) && version > 1;
  if (typeof format === "string" && FORMATS.has(format) && newer) {
    const message = `${format} format_version ${version} is newer than 1`;
    return err(
      seq === undefined
        ? { code: "unsupported_format", message }
        : { code: "unsupported_format", message, seq },
    );
  }
  if (isHead) {
    const head = Head.safeParse(value);
    return head.success
      ? ok({ kind: "head", head: head.data })
      : invalid(describe(head.error), seq);
  }
  const header = Header.safeParse(value);
  return header.success
    ? ok({ kind: "header", header: header.data })
    : invalid(describe(header.error), 0);
}

function parseEventLine(
  value: Record<string, unknown>,
): Result<ParsedLine, ParseError> {
  const known = KnownEvent.safeParse(value);
  if (known.success) return ok({ kind: "event", event: known.data });
  const seq = readSeq(value);
  const unknown = UnknownEvent.safeParse(value);
  if (!unknown.success) return invalid(describe(known.error), seq);
  if (unknown.data.critical) {
    const { type, type_version } = unknown.data;
    return err({
      code: "unsupported_critical_event",
      message: `unknown critical event ${type} v${type_version}`,
      seq: unknown.data.seq,
    });
  }
  return ok({ kind: "unknown_event", event: unknown.data });
}

/**
 * Parses one stored line (without its newline) at the storage trust boundary.
 * Semantic rules across lines (seq, chains, transitions) are not checked here.
 */
export function parseLogLine(line: string): Result<ParsedLine, ParseError> {
  // A UTF-16 unit is at most 3 UTF-8 bytes, so short lines skip the encode.
  if (
    line.length * 3 > MAX_LINE_BYTES &&
    new TextEncoder().encode(line).length > MAX_LINE_BYTES
  ) {
    return invalid("line exceeds 1 MiB");
  }
  const json = parseStrictJson(line);
  if (!json.ok)
    return invalid(`${json.error.message} at offset ${json.error.offset}`);
  // Stored bytes are hashed as written, so a line is admitted only in its canonical form.
  const canonical = canonicalize(json.value);
  if (!canonical.ok || canonical.value !== line) {
    return invalid("line is not in RFC 8785 canonical form");
  }
  if (!isRecord(json.value)) return invalid("a line is a JSON object");
  return "format" in json.value
    ? parseFramingLine(json.value)
    : parseEventLine(json.value);
}

import type { z } from "zod";
import { err, ok, type Result } from "../result";
import { Head, Header } from "./envelope";
import type { ErrorCode } from "./errors";
import { KnownEvent } from "./events";
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

function parseFramingLine(
  value: Record<string, unknown>,
): Result<ParsedLine, ParseError> {
  const { format, format_version: version } = value;
  if (typeof format === "string" && FORMATS.has(format) && version !== 1) {
    return err({
      code: "unsupported_format",
      message: `${format} format_version is not 1`,
    });
  }
  if (format === "threads.head") {
    const head = Head.safeParse(value);
    return head.success
      ? ok({ kind: "head", head: head.data })
      : invalid(describe(head.error));
  }
  const header = Header.safeParse(value);
  return header.success
    ? ok({ kind: "header", header: header.data })
    : invalid(describe(header.error));
}

function parseEventLine(
  value: Record<string, unknown>,
): Result<ParsedLine, ParseError> {
  const known = KnownEvent.safeParse(value);
  if (known.success) return ok({ kind: "event", event: known.data });
  const { seq: rawSeq } = value;
  const seq = typeof rawSeq === "number" ? rawSeq : undefined;
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
  if (!isRecord(json.value)) return invalid("a line is a JSON object");
  return "format" in json.value
    ? parseFramingLine(json.value)
    : parseEventLine(json.value);
}

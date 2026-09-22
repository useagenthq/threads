import type { Head } from "../log";
import { err, ok, type Result } from "../result";
import { addLine, type Chain, emptyChain } from "./chain";
import { type LogError, logError } from "./error";

/** A verified branch export: its segments, resolved chain and fold, and the head outcome. */
export type VerifiedLog = Chain & {
  /** True only when a head checkpoint closed the export and matched it. */
  readonly headVerified: boolean;
  /** Bytes of the valid prefix that readers serve. */
  readonly committedBytes: number;
  /** An unterminated final chunk: an interrupted copy, dropped (wire rule 14). */
  readonly torn:
    | { readonly offset: number; readonly bytes: Uint8Array }
    | undefined;
};

type Split = {
  readonly lines: Uint8Array[];
  readonly torn:
    | { readonly offset: number; readonly bytes: Uint8Array }
    | undefined;
};

/** Splits an export on "\n". Bytes after the last newline are a torn tail candidate. */
function split(bytes: Uint8Array): Split {
  const lines: Uint8Array[] = [];
  let start = 0;
  for (let i = bytes.indexOf(0x0a); i !== -1; i = bytes.indexOf(0x0a, start)) {
    lines.push(bytes.subarray(start, i));
    start = i + 1;
  }
  const rest = bytes.subarray(start);
  return {
    lines,
    torn: rest.length > 0 ? { offset: start, bytes: rest } : undefined,
  };
}

/** Imports an export read-only: every line, the chains, the fork links and the head. */
export function verifyExport(bytes: Uint8Array): Result<VerifiedLog, LogError> {
  const { lines, torn } = split(bytes);
  // "\n" is the commit marker: an unterminated final chunk is torn whatever it holds, even a
  // valid head. Every terminated line must still verify; there is no fallback to a prefix.
  const log = verifyLines(lines);
  if (!log.ok || torn === undefined) return log;
  return ok({ ...log.value, headVerified: false, torn });
}

/** Verifies stored lines in order. An absent head leaves the head unverified. */
export function verifyLines(
  lines: readonly Uint8Array[],
): Result<VerifiedLog, LogError> {
  const chain = emptyChain();
  let head: Head | undefined;
  // Header and event lines with their "\n"; the head line is never counted.
  let committedBytes = 0;
  for (const bytes of lines) {
    if (head !== undefined)
      return err(
        logError("invalid_line", "a line after the head checkpoint", head.seq),
      );
    const added = addLine(chain, bytes);
    if (!added.ok) return added;
    if (added.value.kind === "head") head = added.value.head;
    else committedBytes += bytes.length + 1;
  }
  const ended = checkEnd(chain, head);
  if (ended !== undefined) return err(ended);
  return ok({
    ...chain,
    headVerified: head !== undefined,
    committedBytes,
    torn: undefined,
  });
}

/** Rule 3: the head checkpoint names the last branch, its last seq and its last line. */
function checkEnd(chain: Chain, head: Head | undefined): LogError | undefined {
  const leaf = chain.segments.at(-1);
  if (leaf === undefined) return logError("invalid_line", "no header", 0);
  if (chain.segments.length > 1 && leaf.events.length === 0)
    return logError(
      "invalid_transition",
      "a child segment without its fork",
      chain.fold.seq,
    );
  if (head === undefined) return undefined;
  const last = leaf.events.at(-1)?.hash ?? leaf.hash;
  const matches =
    head.branch_id === leaf.header.branch_id &&
    head.seq === chain.fold.seq &&
    head.hash === last;
  return matches
    ? undefined
    : logError(
        "head_mismatch",
        "the head checkpoint is not the last line",
        head.seq,
      );
}

import { apply } from "../fold/apply";
import { type EventLine, emptyFold, type Fold } from "../fold/state";
import { sha256Hex } from "../hash";
import { type Header, type ParsedLine, parseLogLine } from "../log";
import { err, ok, type Result } from "../result";
import { validateNext } from "../validate";
import { type LogError, logError } from "./error";

/** The envelope fields the chain checks read. */
type ChainFields = {
  readonly seq: number;
  readonly prev_hash: string;
  readonly branch_id: string;
  readonly type: string;
  readonly data: { readonly [key: string]: unknown };
};

/** An event line with its stored bytes and their hash. */
export type ChainEvent = EventLine & {
  readonly bytes: Uint8Array;
  readonly hash: string;
};

/** One branch's own lines: its header and the events it wrote. */
export type Segment = {
  readonly header: Header;
  readonly bytes: Uint8Array;
  readonly hash: string;
  readonly events: ChainEvent[];
};

/** Segments in export order and the resolved chain they form, with its fold. */
export type Chain = {
  readonly segments: Segment[];
  readonly events: ChainEvent[];
  readonly fold: Fold;
};

export function emptyChain(): Chain {
  return { segments: [], events: [], fold: emptyFold() };
}

const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/**
 * Admits one stored line (without its newline) in the conformance order: first the line on its
 * own (UTF-8, JSON admission, canonical form, format admission, full schema incl. the critical
 * rule), then the chain (segment structure, seq, prev_hash, fork links, validate_next). A head
 * line is returned for the caller to check against the end.
 */
export function addLine(
  chain: Chain,
  bytes: Uint8Array,
): Result<ParsedLine, LogError> {
  // An unreadable seq is named by position: 0 for the first line, else the last seq plus 1.
  const position = chain.segments.length === 0 ? 0 : chain.fold.seq + 1;
  let text: string;
  try {
    text = utf8.decode(bytes);
  } catch {
    return err(logError("invalid_line", "line is not UTF-8", position));
  }
  const parsed = parseLogLine(text);
  if (!parsed.ok) {
    // The whole line is checked before the chain (conformance README, step 1).
    const { code, message, seq = position } = parsed.error;
    return err(logError(code, message, seq));
  }
  const line = parsed.value;
  if (line.kind === "header") {
    const hash = sha256Hex(bytes);
    chain.segments.push({ header: line.header, bytes, hash, events: [] });
    return ok(line);
  }
  if (line.kind === "head") return ok(line);
  const added = addEvent(chain, line, bytes);
  return added.ok ? ok(line) : added;
}

function addEvent(
  chain: Chain,
  line: EventLine,
  bytes: Uint8Array,
): Result<ChainEvent, LogError> {
  const linkError = checkLinks(chain, line.event);
  if (linkError !== undefined) return err(linkError);
  const valid = validateNext(chain.fold, line);
  if (!valid.ok) return valid;
  apply(chain.fold, line);
  const event: ChainEvent = { ...line, bytes, hash: sha256Hex(bytes) };
  chain.segments.at(-1)?.events.push(event);
  chain.events.push(event);
  return ok(event);
}

/** The hash of the resolved chain's last line: what a fork at this point binds to. */
export function tipHash(chain: Chain): string | undefined {
  return chain.events.at(-1)?.hash ?? chain.segments[0]?.hash;
}

/** Rules 1, 2 and 4, plus the fork link that opens a child segment. */
function checkLinks(chain: Chain, e: ChainFields): LogError | undefined {
  const segment = chain.segments.at(-1);
  if (segment === undefined)
    return logError("invalid_transition", "an event before any header", e.seq);
  if (e.seq !== chain.fold.seq + 1) {
    const message = `seq ${e.seq} does not follow ${chain.fold.seq}`;
    return logError("seq_mismatch", message, e.seq);
  }
  const previous = segment.events.at(-1)?.hash ?? segment.hash;
  if (e.prev_hash !== previous)
    return logError("prev_hash_mismatch", "prev_hash breaks the chain", e.seq);
  const forkError = checkFork(chain, e);
  if (forkError !== undefined) return forkError;
  if (e.branch_id === segment.header.branch_id) return undefined;
  const message = `branch_id ${e.branch_id} is not its segment's`;
  return logError("invalid_transition", message, e.seq);
}

/** A child segment opens with its fork, bound to the parent's line at at_seq (rule 2). */
function checkFork(chain: Chain, e: ChainFields): LogError | undefined {
  const opensChild =
    chain.segments.length > 1 && chain.segments.at(-1)?.events.length === 0;
  if (!opensChild) {
    return e.type === "fork"
      ? logError("invalid_transition", "fork only opens a child segment", e.seq)
      : undefined;
  }
  if (e.type !== "fork")
    return logError(
      "invalid_transition",
      "a child segment opens with fork",
      e.seq,
    );
  const parent = chain.segments.at(-2)?.header.branch_id;
  const { parent_branch_id: parentId, at_hash: atHash } = e.data;
  const bound = parentId === parent && atHash === tipHash(chain);
  return bound
    ? undefined
    : logError(
        "prev_hash_mismatch",
        "fork is not bound to the parent line at at_seq",
        e.seq,
      );
}

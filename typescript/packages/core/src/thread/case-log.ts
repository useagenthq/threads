import { sha256Hex } from "../hash";
import { canonicalize, type Json, parseStrictJson } from "../log";
import { err, ok, type Result } from "../result";
import { canonicalLine } from "../store/encode";
import type { VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import { VERSION } from "../version";

// A saved case's log (spec/conformance README, "log.jsonl is an export"): the resolved chain
// through the snapshot it restores, once per implementation. Only the implementation a leaf
// header names may append to it, so each file's leaf segment names its runner:
// ancestor segments keep their exact bytes; the leaf's header and the prev_hash chain after it
// are rewritten, exactly as spec/tools/gen_fixtures.py writes a recover or stub pair.

export type Impl = "threads-ts" | "threads-py";

const utf8 = new TextEncoder();
const text = new TextDecoder();

type Segment = VerifiedLog["segments"][number];

/** The segments with their events through `through`; a child forked after it is dropped. */
function through(log: VerifiedLog, seq: number): readonly Segment[] {
  const kept: Segment[] = [];
  for (const [i, segment] of log.segments.entries()) {
    const events = segment.events.filter((e) => e.event.seq <= seq);
    if (i > 0 && events.length === 0) break;
    kept.push({ ...segment, events });
  }
  return kept;
}

function object(bytes: Uint8Array): Result<Record<string, Json>, LogError> {
  const parsed = parseStrictJson(text.decode(bytes));
  if (!parsed.ok || typeof parsed.value !== "object" || parsed.value === null)
    return err(logError("invalid_line", "a stored line is not a JSON object"));
  return Array.isArray(parsed.value)
    ? err(logError("invalid_line", "a stored line is not a JSON object"))
    : ok(parsed.value);
}

function line(value: Json): Result<Uint8Array, LogError> {
  const canonical = canonicalize(value);
  return canonical.ok
    ? ok(utf8.encode(canonical.value))
    : err(logError("invalid_line", "a line is not canonical JSON"));
}

/** The leaf segment's lines as `impl` would have written them: new header, re-linked chain. */
function rewritten(
  leaf: Segment,
  impl: Impl,
): Result<readonly Uint8Array[], LogError> {
  if (leaf.header.writer.impl === impl)
    return ok([leaf.bytes, ...leaf.events.map((e) => e.bytes)]);
  const header = object(leaf.bytes);
  if (!header.ok) return header;
  const head = line({ ...header.value, writer: { impl, version: VERSION } });
  if (!head.ok) return head;
  const lines = [head.value];
  for (const e of leaf.events) {
    const event = object(e.bytes);
    if (!event.ok) return event;
    const prev = lines.at(-1) ?? head.value;
    const next = line({ ...event.value, prev_hash: sha256Hex(prev) });
    if (!next.ok) return next;
    lines.push(next.value);
  }
  return ok(lines);
}

/** The export through seq `seq` whose leaf segment `impl` wrote, ending with its head line. */
export function caseLog(
  log: VerifiedLog,
  seq: number,
  impl: Impl,
): Result<Uint8Array, LogError> {
  const segments = through(log, seq);
  const leaf = segments.at(-1);
  if (leaf === undefined) throw new Error("a verified log has a header");
  const lines = segments
    .slice(0, -1)
    .flatMap((s) => [s.bytes, ...s.events.map((e) => e.bytes)]);
  const own = rewritten(leaf, impl);
  if (!own.ok) return own;
  const last = own.value.at(-1);
  if (last === undefined) throw new Error("a segment has a header line");
  const head = canonicalLine({
    format: "threads.head",
    format_version: 1,
    branch_id: leaf.header.branch_id,
    seq: leaf.events.at(-1)?.event.seq ?? 0,
    hash: sha256Hex(last),
  });
  if (!head.ok) return head;
  const all = [...lines, ...own.value, head.value];
  return ok(utf8.encode(all.map((l) => `${text.decode(l)}\n`).join("")));
}

import { canonicalize, type Json, type KnownEvent } from "../log";

// How a rerun is compared with its recording (spec lane 22, B.2): the appended events after
// normalizing what differs between two runs of the same turn (event ids and the references to
// them, time, epoch, the hash chain), and the case's `must` matchers.

/** case.schema.json EventMatcher: exact on the listed envelope keys, deep subset on data. */
export type Matcher = {
  readonly type: string;
  readonly seq?: number | undefined;
  readonly actor_kind?: string | undefined;
  readonly epoch?: number | undefined;
  readonly branch_id?: string | undefined;
  readonly critical?: boolean | undefined;
  readonly data?: Readonly<Record<string, unknown>> | undefined;
};

/** A matcher against one event: listed envelope keys exactly, data as a deep subset. */
export function matches(m: Matcher, e: KnownEvent): boolean {
  const envelope: readonly [unknown, unknown][] = [
    [m.type, e.type],
    [m.seq, e.seq],
    [m.actor_kind, e.actor.kind],
    [m.epoch, e.epoch],
    [m.branch_id, e.branch_id],
    [m.critical, e.critical],
  ];
  return (
    envelope.every(([want, got]) => want === undefined || want === got) &&
    (m.data === undefined || subset(m.data, e.data))
  );
}

const isObject = (v: unknown): v is object =>
  typeof v === "object" && v !== null && !Array.isArray(v);

function subset(want: unknown, got: unknown): boolean {
  if (!isObject(want) || !isObject(got)) return same(want, got);
  return Object.entries(want).every(([k, v]) => subset(v, Reflect.get(got, k)));
}

function same(a: unknown, b: unknown): boolean {
  const x = canonicalize(JSON.parse(JSON.stringify(a ?? null)));
  const y = canonicalize(JSON.parse(JSON.stringify(b ?? null)));
  return x.ok && y.ok && x.value === y.value;
}

/** Data fields that hold a time: compared as an offset from the event's own time. */
const TIMES: ReadonlySet<string> = new Set([
  "not_before",
  "expires_at",
  "deadline",
  "scheduled_for",
]);

function relabel(
  value: unknown,
  ids: ReadonlyMap<string, string>,
  time: number,
  key = "",
): Json {
  if (typeof value === "string") return ids.get(value) ?? value;
  if (typeof value === "number") return TIMES.has(key) ? value - time : value;
  if (Array.isArray(value)) return value.map((v) => relabel(v, ids, time));
  if (!isObject(value)) return typeof value === "boolean" ? value : null;
  return Object.fromEntries(
    Object.entries(value).map(([k, v]) => [k, relabel(v, ids, time, k)]),
  );
}

/**
 * Each event as the comparison sees it: its event id, and every reference to an id of the
 * list, becomes its position; time, epoch and prev_hash are dropped.
 */
export function normalized(events: readonly KnownEvent[]): readonly string[] {
  const ids = new Map(events.map((e, i) => [e.event_id, `#${i}`]));
  return events.map((e) => {
    const text = canonicalize({
      seq: e.seq,
      type: e.type,
      type_version: e.type_version,
      critical: e.critical,
      actor: relabel(e.actor, ids, e.time),
      data: relabel(e.data, ids, e.time),
    });
    if (!text.ok) throw new Error("an event is canonical JSON");
    return text.value;
  });
}

/** Render v1 line 0 of a request artifact: its bytes up to the first newline. */
export function firstLine(bytes: Uint8Array): Uint8Array {
  const end = bytes.indexOf(0x0a);
  return end === -1 ? bytes : bytes.slice(0, end);
}

export type Mismatch = {
  readonly index: number;
  readonly want: string | null;
  readonly got: string | null;
};

/** The first appended event that differs from the recording, if any. */
export function firstMismatch(
  recorded: readonly KnownEvent[],
  rerun: readonly KnownEvent[],
): Mismatch | undefined {
  const want = normalized(recorded);
  const got = normalized(rerun);
  const length = Math.max(want.length, got.length);
  for (let index = 0; index < length; index += 1)
    if (want[index] !== got[index])
      return {
        index,
        want: recorded[index]?.type ?? null,
        got: rerun[index]?.type ?? null,
      };
  return undefined;
}

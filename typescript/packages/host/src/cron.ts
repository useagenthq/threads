import { err, ok, type Result } from "@threads/core/host";

// Five-field cron (minute hour day-of-month month day-of-week) in an IANA time zone (v2
// F10). Wall-clock rules: a nonexistent local time (spring forward) runs at the first valid
// instant after it; an ambiguous one (fall back) runs once, at its first instance.

export type Cron = {
  readonly fields: readonly (ReadonlySet<number> | undefined)[];
};

const RANGES: readonly (readonly [number, number])[] = [
  [0, 59],
  [0, 23],
  [1, 31],
  [1, 12],
  [0, 7],
];

export function parseCron(expr: string): Result<Cron, string> {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return err(`cron needs 5 fields: ${expr}`);
  const fields: (ReadonlySet<number> | undefined)[] = [];
  for (const [i, part] of parts.entries()) {
    const [lo, hi] = RANGES[i] ?? [0, 0];
    const set = field(part, lo, hi);
    if (set === null) return err(`bad cron field ${part}`);
    // Sunday is 0 or 7.
    if (i === 4 && set?.has(7) === true) set.add(0);
    fields.push(set);
  }
  return ok({ fields });
}

/** A field's values; undefined for `*` (unrestricted); null when malformed. */
function field(
  part: string,
  lo: number,
  hi: number,
): Set<number> | undefined | null {
  if (part === "*") return undefined;
  const out = new Set<number>();
  for (const item of part.split(",")) {
    const range = rangeOf(item, lo, hi);
    if (range === null) return null;
    for (let v = range.from; v <= range.to; v += range.step) out.add(v);
  }
  return out;
}

/** `*`, `n`, `a-b`, each with an optional `/step`. */
function rangeOf(
  item: string,
  lo: number,
  hi: number,
): {
  readonly from: number;
  readonly to: number;
  readonly step: number;
} | null {
  const m = /^(\*|\d+)(?:-(\d+))?(?:\/(\d+))?$/.exec(item);
  if (m === null) return null;
  const star = m[1] === "*";
  const from = star ? lo : Number(m[1]);
  const open = star || m[3] !== undefined ? hi : from;
  const to = m[2] === undefined ? open : Number(m[2]);
  const step = m[3] === undefined ? 1 : Number(m[3]);
  return from < lo || to > hi || from > to || step < 1
    ? null
    : { from, to, step };
}

type Wall = {
  readonly minute: number;
  readonly hour: number;
  readonly day: number;
  readonly month: number;
  readonly weekday: number;
};

function matches(cron: Cron, w: Wall): boolean {
  const [minute, hour, dom, month, dow] = cron.fields;
  const has = (s: ReadonlySet<number> | undefined, v: number): boolean =>
    s === undefined || s.has(v);
  if (!has(minute, w.minute) || !has(hour, w.hour) || !has(month, w.month))
    return false;
  // Standard cron: with both day fields restricted, either may match.
  if (dom !== undefined && dow !== undefined)
    return dom.has(w.day) || dow.has(w.weekday);
  return has(dom, w.day) && has(dow, w.weekday);
}

/** The zone's wall clock at `ms`, as a UTC-epoch number of the same fields (minute precision). */
function localEpoch(format: Intl.DateTimeFormat, ms: number): number {
  const parts: Record<string, number> = {};
  for (const p of format.formatToParts(ms))
    if (p.type !== "literal") parts[p.type] = Number(p.value);
  return Date.UTC(
    parts["year"] ?? 0,
    (parts["month"] ?? 1) - 1,
    parts["day"] ?? 1,
    parts["hour"] ?? 0,
    parts["minute"] ?? 0,
  );
}

function wall(local: number): Wall {
  const d = new Date(local);
  return {
    minute: d.getUTCMinutes(),
    hour: d.getUTCHours(),
    day: d.getUTCDate(),
    month: d.getUTCMonth() + 1,
    weekday: d.getUTCDay(),
  };
}

const MINUTE = 60_000;

/** Occurrence instants (UTC ms) in (from, to], minute by minute in the zone's wall clock. */
export function occurrences(
  cron: Cron,
  timezone: string,
  from: number,
  to: number,
): readonly number[] {
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    hourCycle: "h23",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
  });
  const out: number[] = [];
  const seen = new Set<number>();
  let t = Math.floor(from / MINUTE) * MINUTE + MINUTE;
  let prev = localEpoch(format, t - MINUTE);
  for (; t <= to; t += MINUTE) {
    const local = localEpoch(format, t);
    if (repeated(format, t, local)) seen.add(local);
    if (due(cron, prev, local, seen)) out.push(t);
    prev = local;
  }
  return out;
}

// ponytail: looks back 3 h for the clock turning back; zones whose offset drops by more than
// that in one step would need a longer look-back.
const LOOK_BACK = 3 * 60 * MINUTE;

/** Whether wall time `local` already happened at an earlier instant (a fall-back repeat), even
 *  when that earlier instant is before the scan started. */
function repeated(
  format: Intl.DateTimeFormat,
  t: number,
  local: number,
): boolean {
  const drop =
    localEpoch(format, t - LOOK_BACK) - (t - LOOK_BACK) - (local - t);
  return drop > 0 && localEpoch(format, t - drop) === local;
}

/** This instant fires: its wall minute matches for the first time, or a skipped one did. */
function due(
  cron: Cron,
  prev: number,
  local: number,
  seen: Set<number>,
): boolean {
  let fire = false;
  // Spring forward: wall minutes between prev and local never happened; they run now.
  for (let skipped = prev + MINUTE; skipped < local; skipped += MINUTE)
    if (matches(cron, wall(skipped)) && !seen.has(skipped)) {
      seen.add(skipped);
      fire = true;
    }
  if (matches(cron, wall(local)) && !seen.has(local)) {
    seen.add(local);
    fire = true;
  }
  return fire;
}

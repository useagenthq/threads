import { ASSIGNED, FOLD, SEPARATORS, WORD } from "./generated/fold-table";

// tool_search matching (spec/schema/README.md, "Deferred tools and tool_search"; reference:
// spec/tools/fixtures/ucd.py and search_ref.py). Every Unicode decision comes from the table
// generated from the pinned 15.0.0 data, never from the runtime's own Unicode tables: none
// of its case, whitespace or edge-stripping helpers is called here (a test greps for them).

export const NO_MATCH = "no deferred tool matches";
/** At most this many exact names load at once, whatever `limit` says. */
const MAX_EXACT = 10;
/** The longest query, in code points. */
export const MAX_QUERY = 200;

export type Candidate = { readonly name: string; readonly description: string };
export type Found = {
  /** The result text's lines, in result order. */
  readonly lines: readonly string[];
  /** The tools to load, in result order. */
  readonly loaded: readonly string[];
};

function within(
  ranges: readonly (readonly [number, number])[],
  cp: number,
): boolean {
  let lo = 0;
  let hi = ranges.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const range = ranges[mid];
    if (range === undefined) return false;
    if (cp < range[0]) hi = mid - 1;
    else if (cp > range[1]) lo = mid + 1;
    else return true;
  }
  return false;
}

const codePoints = (s: string): number[] =>
  Array.from(s, (c) => c.codePointAt(0) ?? 0);

/** Step 1: a code point not assigned in 15.0.0 (or a surrogate) becomes U+0020. */
function assignedOnly(s: string): string {
  return codePoints(s)
    .map((cp) => (within(ASSIGNED, cp) ? String.fromCodePoint(cp) : " "))
    .join("");
}

/** The normative fold: step 1, NFKC, full case folding from the table, NFKC. */
export function fold(s: string): string {
  const folded = codePoints(assignedOnly(s).normalize("NFKC"))
    .map((cp) => FOLD.get(cp) ?? String.fromCodePoint(cp))
    .join("");
  return folded.normalize("NFKC");
}

/** Maximal runs of code points for which `keep` holds, in order. */
function runs(s: string, keep: (cp: number) => boolean): string[] {
  const out: string[] = [];
  let cur = "";
  for (const cp of codePoints(s)) {
    if (keep(cp)) cur += String.fromCodePoint(cp);
    else if (cur !== "") {
      out.push(cur);
      cur = "";
    }
  }
  if (cur !== "") out.push(cur);
  return out;
}

/** The exact-name split: step 1 and NFKC, then the pinned separators, empty terms dropped. */
export function terms(query: string): string[] {
  return runs(
    assignedOnly(query).normalize("NFKC"),
    (cp) => !SEPARATORS.has(cp),
  );
}

/** Maximal runs of General_Category L, M or N code points of fold(s). */
export function tokens(s: string): string[] {
  return runs(fold(s), (cp) => within(WORD, cp));
}

const firstLine = (description: string): string =>
  description.split("\n", 1)[0] ?? "";

const described = (c: Candidate): string =>
  `${c.name}: ${firstLine(c.description)}`;

/**
 * `deferred`: every still-deferred tool, in tool-set order; `others`: every other current
 * tool's name. Exact names first, then keyword ranking, else no match.
 */
export function search(
  query: string,
  limit: number,
  deferred: readonly Candidate[],
  others: readonly string[],
): Found {
  const exact = exactNames(query, deferred, others);
  if (exact !== undefined) return exact;
  const ranked = rank(query, limit, deferred);
  return ranked.length > 0
    ? { lines: ranked.map(described), loaded: ranked.map((c) => c.name) }
    : { lines: [NO_MATCH], loaded: [] };
}

function exactNames(
  query: string,
  deferred: readonly Candidate[],
  others: readonly string[],
): Found | undefined {
  const names = new Map<string, string>([
    ...deferred.map((c): [string, string] => [fold(c.name), c.name]),
    ...others.map((n): [string, string] => [fold(n), n]),
  ]);
  const found = terms(query).map((t) => names.get(fold(t)));
  if (found.length === 0 || found.includes(undefined)) return undefined;
  const byName = new Map(deferred.map((c) => [c.name, c]));
  const lines: string[] = [];
  const loaded: string[] = [];
  for (const name of new Set(found)) {
    if (name === undefined) continue;
    const c = byName.get(name);
    if (c === undefined) lines.push(`${name}: already loaded`);
    else if (loaded.length < MAX_EXACT) {
      loaded.push(name);
      lines.push(described(c));
    }
  }
  return { lines, loaded };
}

/** Code-point order (JS's default string order compares UTF-16 units). */
export const byCodePoint = (a: string, b: string): number => {
  const x = codePoints(a);
  const y = codePoints(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = (x[i] ?? 0) - (y[i] ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
};

function rank(
  query: string,
  limit: number,
  deferred: readonly Candidate[],
): Candidate[] {
  const wanted = new Set(tokens(query));
  return deferred
    .map((c) => ({
      c,
      score: new Set(tokens(`${c.name} ${c.description}`)).intersection(wanted)
        .size,
    }))
    .filter((x) => x.score > 0)
    .toSorted((a, b) => b.score - a.score || byCodePoint(a.c.name, b.c.name))
    .slice(0, limit)
    .map((x) => x.c);
}

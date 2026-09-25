import { expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

// The portable statement subset (spec/schema/README.md, "Storage"): every store statement runs
// on SQLite and Postgres. This scan fails on the forms only SQLite has, and on a statement
// issued outside a transaction (only a `tx` runs statements).

const PACKAGES = join(import.meta.dir, "../../..");
const ROOTS = ["core/src", "host/src", "cli/src", "postgres/src"];
/** The SQLite driver and the SQLite-only FTS5 providers may use SQLite's own forms. */
const SQLITE_ONLY = [
  "core/src/store/bun-sqlite.ts",
  "core/src/memory/local-memory.ts",
  "core/src/memory/local-knowledge.ts",
  "core/src/memory/rebuild.ts",
];

const BANNED: readonly (readonly [string, RegExp])[] = [
  ["changes()", /\bchanges\(\)/i],
  ["IS ? (use IS NOT DISTINCT FROM ?)", /\bIS \?/],
  ["INSERT OR", /\bINSERT OR\b/],
  ["PRAGMA", /\bPRAGMA\b/],
  ["datetime(", /\bdatetime\(/],
  ["unixepoch(", /\bunixepoch\(/],
  ["SELECT * (name the columns)", /SELECT \*/],
  ["two-argument max( or min(", /[^.\w](max|min)\([^()]*,[^()]*\)/i],
  ["a statement outside a transaction", /\b(db|store\.driver)\.(run|all)\(/],
];

function files(dir: string): readonly string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((d) => {
    const path = join(dir, d.name);
    if (d.isDirectory()) return d.name === "generated" ? [] : files(path);
    return d.name.endsWith(".ts") ? [path] : [];
  });
}

/** The SQL a file holds: its string and template literals. */
function literals(source: string): readonly string[] {
  return source.match(/`[^`]*`|"(?:[^"\\\n]|\\.)*"/g) ?? [];
}

/** The banned forms one source file holds. */
function banned(rel: string, source: string): readonly string[] {
  const sql = literals(source).filter((s) =>
    /\b(SELECT|INSERT|UPDATE|DELETE)\b/.test(s),
  );
  return BANNED.flatMap(([name, pattern]) => {
    const where = name === "a statement outside a transaction" ? [source] : sql;
    return where.some((s) => pattern.test(s)) ? [`${rel}: ${name}`] : [];
  });
}

test("every store statement is in the portable subset", () => {
  const found = ROOTS.flatMap((root) =>
    files(join(PACKAGES, root)).flatMap((path) => {
      const rel = path.slice(PACKAGES.length + 1);
      return SQLITE_ONLY.includes(rel)
        ? []
        : banned(rel, readFileSync(path, "utf8"));
    }),
  );
  expect(found).toEqual([]);
});

test("the scan finds each banned form", () => {
  const samples = [
    "SELECT changes() AS n",
    "SELECT 1 FROM t WHERE a IS ?",
    "INSERT OR IGNORE INTO t VALUES (1)",
    "PRAGMA user_version",
    "SELECT datetime('now')",
    "SELECT unixepoch()",
    "SELECT * FROM t",
    "UPDATE t SET a = max(a, b)",
    "db.run(",
  ];
  for (const [i, [, pattern]] of BANNED.entries())
    expect(pattern.test(samples[i] ?? "")).toBe(true);
  expect(
    /[^.\w](max|min)\([^()]*,[^()]*\)/i.test("SELECT MAX(a) FROM t, u"),
  ).toBe(false);
});

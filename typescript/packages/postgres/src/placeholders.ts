// The portable subset's `?` placeholders in pg's form, $1..$n, rewritten outside quoted literals
// and identifiers (spec/conformance/vectors/sql-placeholders.json). A literal `%` is left alone:
// only psycopg needs it doubled.

const cache = new Map<string, string>();

export function numbered(sql: string): string {
  const found = cache.get(sql);
  if (found !== undefined) return found;
  let out = "";
  let quote: string | undefined;
  let n = 0;
  for (const char of sql) {
    if (quote !== undefined) {
      if (char === quote) quote = undefined;
      out += char;
    } else if (char === "'" || char === '"') {
      quote = char;
      out += char;
    } else if (char === "?") {
      n += 1;
      out += `$${n}`;
    } else out += char;
  }
  cache.set(sql, out);
  return out;
}

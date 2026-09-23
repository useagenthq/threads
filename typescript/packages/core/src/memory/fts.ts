import { ConfigError } from "../agent/errors";
import type { SqliteDriver } from "../store/driver";

// SQLite FTS5 for the built-in providers (FTS5 is a setup check).

/** Creates the provider's tables; a SQLite build without FTS5 is a setup error that says so. */
export function installFts(db: SqliteDriver, ddl: string, who: string): void {
  try {
    db.exec(ddl);
  } catch (error) {
    const text = String(error);
    throw new ConfigError(
      "capability_missing",
      /fts5/i.test(text)
        ? `${who} needs SQLite with the FTS5 extension, and this SQLite build lacks it: ${text}`
        : `${who}: can't create its tables: ${text}`,
    );
  }
}

/**
 * A MATCH expression from free text: each word quoted as a literal term, any word matching.
 * Query text never reaches FTS5 syntax, so it can't form an operator or a column filter.
 */
export function matchQuery(text: string): string | undefined {
  const words = text.match(/[\p{L}\p{N}_]+/gu) ?? [];
  if (words.length === 0) return undefined;
  return words.map((w) => `"${w}"`).join(" OR ");
}

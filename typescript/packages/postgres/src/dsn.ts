import { ConfigError } from "@threads/core/adapter";

// The connection URL holds a password (DATABASE_URL): no error may carry it. A string that isn't
// a postgres:// URL is refused without echoing it; the driver's own errors are scrubbed of the
// URL and its password before they leave the store.

/** The URL, parsed; invalid_config, naming nothing of it, when it isn't a postgres:// URL. */
export function postgresUrl(dsn: string): URL {
  const url = URL.canParse(dsn) ? new URL(dsn) : undefined;
  if (
    url === undefined ||
    (url.protocol !== "postgres:" && url.protocol !== "postgresql:")
  )
    throw new ConfigError(
      "invalid_config",
      "postgres() needs a postgres:// URL",
    );
  return url;
}

/** Removes `dsn` and its password from `error`'s message and stack, and every cause's. */
export function scrubbed(error: unknown, dsn: string): unknown {
  const secrets = secretsOf(dsn);
  for (let at = error; at instanceof Error; at = at.cause) {
    at.message = scrub(at.message, secrets);
    if (at.stack !== undefined) at.stack = scrub(at.stack, secrets);
  }
  return error;
}

function secretsOf(dsn: string): readonly string[] {
  const password = URL.canParse(dsn) ? new URL(dsn).password : "";
  return [dsn, password, safeDecode(password)]
    .filter((s) => s.length > 0)
    .toSorted((a, b) => b.length - a.length);
}

function safeDecode(text: string): string {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

function scrub(text: string, secrets: readonly string[]): string {
  return secrets.reduce((out, s) => out.replaceAll(s, "[redacted]"), text);
}

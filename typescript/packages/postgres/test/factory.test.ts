import { describe, expect, test } from "bun:test";
import { ConfigError } from "@threads/core/adapter";
import { StoreError } from "@threads/core/store-driver";
import { openStore, Store } from "../../core/src/agent/sqlite";
import { postgres } from "../src";

// postgres() (spec/api.json): lazy, a URL or DATABASE_URL, and invalid_config with neither. None
// of these needs a server: a URL nobody listens on is an outage at first use, not a setup error.

const NOBODY = "postgres://threads:threads@127.0.0.1:1/threads";

async function withEnv<T>(
  value: string | undefined,
  run: () => Promise<T>,
): Promise<T> {
  const saved = process.env["DATABASE_URL"];
  if (value === undefined) delete process.env["DATABASE_URL"];
  else process.env["DATABASE_URL"] = value;
  try {
    return await run();
  } finally {
    if (saved === undefined) delete process.env["DATABASE_URL"];
    else process.env["DATABASE_URL"] = saved;
  }
}

/** An error's message, stack and every cause's, and its own enumerable fields. */
function everything(error: unknown): string {
  const parts: string[] = [];
  for (let at = error; at !== undefined && at !== null; ) {
    parts.push(String(at));
    if (!(at instanceof Error)) break;
    parts.push(at.stack ?? "", JSON.stringify({ ...at }));
    at = at.cause;
  }
  return parts.join("\n");
}

describe("postgres()", () => {
  test("is lazy: it returns the opaque Store and connects nothing", () => {
    expect(postgres(NOBODY)).toBeInstanceOf(Store);
  });

  test("without a URL or DATABASE_URL, the first use is invalid_config naming DATABASE_URL", async () => {
    const opened = await withEnv(undefined, () =>
      openStore(postgres()).catch((error: unknown) => error),
    );
    expect(opened).toBeInstanceOf(ConfigError);
    expect(String(opened)).toContain(
      "postgres() needs a URL: pass one or set DATABASE_URL",
    );
  });

  test("a key=value DATABASE_URL is invalid_config, and its password is in no part of the error", async () => {
    const opened = await withEnv(
      "host=127.0.0.1 port=1 user=threads password=SEKRET-kv dbname=threads",
      () => openStore(postgres()).catch((error: unknown) => error),
    );
    expect(opened).toBeInstanceOf(ConfigError);
    expect(String(opened)).toContain("postgres() needs a postgres:// URL");
    expect(everything(opened)).not.toContain("SEKRET-kv");
  });

  test("a URL's password is in no part of the error: a malformed URL, and a server nobody runs", async () => {
    for (const url of [
      "postgres://threads:SEKRET-url@127.0.0.1:notaport/threads",
      "postgres://threads:SEKRET%2Durl@127.0.0.1:1/threads",
    ]) {
      const opened = await openStore(postgres(url)).catch(
        (error: unknown) => error,
      );
      expect(opened).toBeInstanceOf(Error);
      expect(everything(opened)).not.toContain("SEKRET");
    }
  });

  test("url defaults to DATABASE_URL", async () => {
    const opened = await withEnv(NOBODY, () =>
      openStore(postgres()).catch((error: unknown) => error),
    );
    // It tried the variable's server: an outage there, never the missing-URL setup error.
    expect(opened).toBeInstanceOf(StoreError);
  });
});

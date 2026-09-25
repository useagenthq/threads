import { ConfigError } from "@threads/core/adapter";
import { type Store, storeOver } from "@threads/core/store-driver";
import { pgArtifacts } from "./artifacts";
import { openPg } from "./driver";

// postgres() (spec/api.json): the log and artifact store on Postgres 16 or later, for hosts on
// several processes or machines. The same opaque Store every API takes; nothing connects until
// first use.

export type { Store } from "@threads/core/store-driver";

/** A postgres:// URL, or DATABASE_URL. With neither, the first use is invalid_config. */
export function postgres(url?: string): Store {
  return storeOver("postgres", async () => {
    const found = url ?? process.env["DATABASE_URL"];
    if (found === undefined || found === "")
      throw new ConfigError(
        "invalid_config",
        "postgres() needs a URL: pass one or set DATABASE_URL",
      );
    const db = openPg(found);
    return { db, artifacts: pgArtifacts(db) };
  });
}

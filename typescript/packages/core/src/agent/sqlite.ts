import { mkdirSync } from "node:fs";
import { join } from "node:path";
import {
  type ArtifactStore,
  fileArtifacts,
  LogStore,
  memoryArtifacts,
} from "../store";

// sqlite() (spec/api.json): the one log and artifact store. Opening is lazy, so
// sqlite() itself does no I/O; the bun:sqlite driver is loaded on first use, which keeps core's
// import graph free of runtime-specific modules.

export type OpenStore = {
  readonly log: LogStore;
  readonly artifacts: ArtifactStore;
};

/** The SQLite log and artifact store. Sealed in v0.1: no public methods. */
export class Store {
  readonly path: string;

  constructor(path: string) {
    this.path = path;
  }
}

const opened = new WeakMap<Store, Promise<OpenStore>>();

export function sqlite(path: string): Store {
  return new Store(path);
}

/** Opens the store once; every run on it shares the same connection. */
export function openStore(store: Store): Promise<OpenStore> {
  let open = opened.get(store);
  if (open === undefined) {
    open = connect(store.path);
    opened.set(store, open);
  }
  return open;
}

async function connect(path: string): Promise<OpenStore> {
  const { openBunSqlite } = await import("../store/bun-sqlite");
  const memory = path === ":memory:";
  if (!memory) mkdirSync(path, { recursive: true, mode: 0o700 });
  const artifacts = memory
    ? memoryArtifacts()
    : fileArtifacts(join(path, "artifacts"));
  const db = openBunSqlite(memory ? path : join(path, "log.db"));
  const log = LogStore.open(db, Date.now, artifacts);
  if (!log.ok) throw new Error(`store ${path}: ${log.error.message}`);
  return { log: log.value, artifacts };
}

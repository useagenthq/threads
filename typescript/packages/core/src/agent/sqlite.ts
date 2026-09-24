import { mkdirSync } from "node:fs";
import { join } from "node:path";
import {
  type ArtifactStore,
  fileArtifacts,
  LogStore,
  memoryArtifacts,
  type SqliteDriver,
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

/** The connection under a root store, and the clock its logs read. */
type Connection = { readonly db: SqliteDriver; readonly now: () => number };

const opened = new WeakMap<Store, Promise<OpenStore>>();
const connections = new WeakMap<Store, Connection>();
/** Tenant views: the root's connection, a log bound to one tenant. */
const views = new WeakMap<
  Store,
  { readonly root: Store; readonly tenant: string }
>();
const byTenant = new WeakMap<Store, Map<string, Store>>();

export function sqlite(path: string): Store {
  return new Store(path);
}

/** Opens the store once; every run on it shares the same connection. */
export function openStore(store: Store): Promise<OpenStore> {
  let open = opened.get(store);
  if (open === undefined) {
    const view = views.get(store);
    open = view === undefined ? connect(store) : scoped(view.root, view.tenant);
    opened.set(store, open);
  }
  return open;
}

/**
 * The same store as one tenant sees it: every thread, branch and ledger row read or written
 * through it is that tenant's. The host opens each caller's store this way.
 */
export function tenantStore(store: Store, tenant: string): Store {
  const root = views.get(store)?.root ?? store;
  let tenants = byTenant.get(root);
  if (tenants === undefined) {
    tenants = new Map();
    byTenant.set(root, tenants);
  }
  let view = tenants.get(tenant);
  if (view === undefined) {
    view = new Store(root.path);
    views.set(view, { root, tenant });
    tenants.set(tenant, view);
  }
  return view;
}

/** The connection and clock under a store, for the host's own tables (inbox, receipts). */
export async function storeConnection(store: Store): Promise<Connection> {
  const root = views.get(store)?.root ?? store;
  await openStore(root);
  const connection = connections.get(root);
  if (connection === undefined)
    throw new Error("this store was built without a driver");
  return connection;
}

async function scoped(root: Store, tenant: string): Promise<OpenStore> {
  const { artifacts } = await openStore(root);
  const { db, now } = await storeConnection(root);
  const log = LogStore.open(db, now, artifacts, tenant);
  if (!log.ok) throw new Error(`store ${root.path}: ${log.error.message}`);
  return { log: log.value, artifacts };
}

async function connect(store: Store): Promise<OpenStore> {
  const { path } = store;
  const { openBunSqlite } = await import("../store/bun-sqlite");
  const memory = path === ":memory:";
  if (!memory) mkdirSync(path, { recursive: true, mode: 0o700 });
  const artifacts = memory
    ? memoryArtifacts()
    : fileArtifacts(join(path, "artifacts"));
  const db = openBunSqlite(memory ? path : join(path, "threads.db"));
  connections.set(store, { db, now: Date.now });
  const log = LogStore.open(db, Date.now, artifacts);
  if (!log.ok) throw new Error(`store ${path}: ${log.error.message}`);
  return { log: log.value, artifacts };
}

/**
 * A private in-memory store on an injected clock, which the caller closes: what an eval rerun
 * imports a case into, so nothing it does reaches a store the user passed.
 */
export async function memoryStore(
  now: () => number,
): Promise<OpenStore & { readonly close: () => void }> {
  const { openBunSqlite } = await import("../store/bun-sqlite");
  const db = openBunSqlite(":memory:");
  const artifacts = memoryArtifacts();
  const log = LogStore.open(db, now, artifacts);
  if (!log.ok) throw new Error(`memory store: ${log.error.message}`);
  return { log: log.value, artifacts, close: () => db.close() };
}

/**
 * A Store over a log and artifact store already open, for the test kit and embedders. With
 * `connection`, tenant views and the host's tables work on it too, on its injected clock.
 */
export function storeOf(open: OpenStore, connection?: Connection): Store {
  const store = new Store(":memory:");
  opened.set(store, Promise.resolve(open));
  if (connection !== undefined) connections.set(store, connection);
  return store;
}

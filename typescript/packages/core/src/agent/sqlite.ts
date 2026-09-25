import { mkdirSync } from "node:fs";
import { join } from "node:path";
import {
  type ArtifactStore,
  fileArtifacts,
  LogStore,
  memoryArtifacts,
  type StoreDriver,
} from "../store";

// sqlite() (spec/api.json): the SQLite log and artifact store. Opening is lazy, so sqlite()
// itself does no I/O; the bun:sqlite driver is loaded on first use, which keeps core's import
// graph free of runtime-specific modules. postgres() (@threads/postgres) builds the same opaque
// Store over its own driver with `storeOver`.

export type OpenStore = {
  readonly log: LogStore;
  readonly artifacts: ArtifactStore;
};

/** The log and artifact store. Opaque: no public methods. */
export class Store {
  readonly path: string;

  constructor(path: string) {
    this.path = path;
  }
}

/** The connection under a root store, and the clock its logs read. */
type Connection = { readonly db: StoreDriver; readonly now: () => number };

/** How a root store opens on first use: its connection and artifacts. */
export type Opener = () => Promise<{
  readonly db: StoreDriver;
  readonly artifacts: ArtifactStore;
}>;

const opened = new WeakMap<Store, Promise<OpenStore>>();
const connections = new WeakMap<Store, Connection>();
const openers = new WeakMap<Store, Opener>();
const clocks = new WeakMap<Store, () => number>();
/** Tenant views: the root's connection, a log bound to one tenant. */
const views = new WeakMap<
  Store,
  { readonly root: Store; readonly tenant: string }
>();
const byTenant = new WeakMap<Store, Map<string, Store>>();

export function sqlite(path: string): Store {
  return new Store(path);
}

/**
 * A Store that opens with `open` on first use (the Postgres package's). `label` names it in
 * errors; `now` is its clock.
 */
export function storeOver(
  label: string,
  open: Opener,
  now: () => number = Date.now,
): Store {
  const store = new Store(label);
  openers.set(store, open);
  clocks.set(store, now);
  return store;
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
  const { log, artifacts } = await openStore(root);
  return { log: log.scoped(tenant), artifacts };
}

async function connect(store: Store): Promise<OpenStore> {
  const { db, artifacts } = await (openers.get(store) ?? sqliteOpener(store))();
  const now = clocks.get(store) ?? Date.now;
  connections.set(store, { db, now });
  const log = await LogStore.open(db, now, artifacts);
  if (!log.ok) {
    await db.close();
    throw new Error(`store ${store.path}: ${log.error.message}`);
  }
  return { log: log.value, artifacts };
}

function sqliteOpener(store: Store): Opener {
  return async () => {
    const { path } = store;
    const { openBunSqlite } = await import("../store/bun-sqlite");
    const memory = path === ":memory:";
    if (!memory) mkdirSync(path, { recursive: true, mode: 0o700 });
    const artifacts = memory
      ? memoryArtifacts()
      : fileArtifacts(join(path, "artifacts"));
    return {
      db: openBunSqlite(memory ? path : join(path, "threads.db")),
      artifacts,
    };
  };
}

/** The driver dialect under a store, once open: memory and knowledge refuse Postgres. */
export async function storeDialect(
  store: Store,
): Promise<StoreDriver["dialect"]> {
  return (await storeConnection(store)).db.dialect;
}

/**
 * A private in-memory store on an injected clock, which the caller closes: what an eval rerun
 * imports a case into, so nothing it does reaches a store the user passed.
 */
export async function memoryStore(
  now: () => number,
): Promise<OpenStore & { readonly close: () => Promise<void> }> {
  const { openBunSqlite } = await import("../store/bun-sqlite");
  const db = openBunSqlite(":memory:");
  const artifacts = memoryArtifacts();
  const log = await LogStore.open(db, now, artifacts);
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

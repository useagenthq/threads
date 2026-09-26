import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { importThread, openThread, type Store, sqlite } from "@threads/core";
import {
  BranchId,
  collect,
  deleteTenant,
  deleteThread,
  openStore,
  storeConnection,
  sweepArtifacts,
  ThreadId,
  tenantStore,
} from "@threads/core/host";
import { type Host, hostSandboxes } from "@threads/host";
import { type EvalArgs, evals } from "./eval";

// The `threads` CLI (spec/api.json cli): thin wrappers over library calls, never a feature of its
// own. Every command answers an exit code; output goes through `io`.

export type Io = {
  readonly out: (text: string) => void;
  readonly bytes: (data: Uint8Array) => void;
  readonly err: (text: string) => void;
};

const USAGE = `usage: threads <command> [options]
  dev [module] [--port N]           run a host module locally; prints each channel's webhook URL
  start [module] [--port N]         the same, for production
  timeline <thread_id> [--branch B] print a branch's steps as JSON lines
  export <branch_id> [--bundle D]   write a branch's JSONL export to stdout, or a portable
                                    bundle (log.jsonl, artifacts/, bundle.json) to D
  import <dir|file>                 verify and store a bundle or a bare JSONL export
  repair <branch_id>                record log_repaired on an imported torn branch
  delete <thread_id> | --tenant T   delete a thread, or every thread of a tenant
  gc [module] [--grace-days N]      release collectable resources, sweep unreferenced artifacts
  eval [--agent M] [--cases D] [--case N]... [--live] [--store D] [--strict] [--out F]
                                    run saved cases: replay and rerun for free, drift with
                                    --agent, a judged live run with --live
options: --store <dir or postgres:// URL> (default .threads), --tenant <id> (default local)`;

const OPTIONS = {
  store: { type: "string" },
  tenant: { type: "string" },
  branch: { type: "string" },
  bundle: { type: "string" },
  port: { type: "string", default: "8787" },
  "grace-days": { type: "string", default: "7" },
  agent: { type: "string" },
  cases: { type: "string", default: "cases" },
  case: { type: "string", multiple: true },
  live: { type: "boolean", default: false },
  strict: { type: "boolean", default: false },
  out: { type: "string" },
} as const;

type Parsed = {
  readonly command: string | undefined;
  readonly args: readonly string[];
  readonly store: string;
  readonly tenant: string | undefined;
  readonly branch: string | undefined;
  readonly bundle: string | undefined;
  readonly port: number;
  readonly graceDays: number;
  readonly eval: EvalArgs;
};

function parse(argv: readonly string[]): Parsed {
  const { values, positionals } = parseArgs({
    args: [...argv],
    options: OPTIONS,
    allowPositionals: true,
  });
  const [command, ...args] = positionals;
  return {
    command,
    args,
    store: values.store ?? ".threads",
    tenant: values.tenant,
    bundle: values.bundle,
    branch: values.branch,
    port: Number(values.port),
    graceDays: Number(values["grace-days"]),
    // eval keeps its threads only in a --store it is given: its default is in memory.
    eval: {
      agent: values.agent,
      cases: values.cases,
      only: values.case ?? [],
      live: values.live,
      store: values.store,
      strict: values.strict,
      out: values.out,
    },
  };
}

export type Served = {
  readonly url: string;
  readonly stop: () => Promise<void>;
};

/** Runs one command; `dev` and `start` hand back the running server through `serving`. */
export async function run(
  argv: readonly string[],
  io: Io,
  serving?: (served: Served) => void,
): Promise<number> {
  let p: Parsed;
  try {
    p = parse(argv);
  } catch (error) {
    io.err(`${String(error)}\n${USAGE}`);
    return 2;
  }
  switch (p.command) {
    case "dev":
    case "start":
      return serve(p, io, serving);
    case "timeline":
      return timeline(p, io);
    case "export":
      return exportBranch(p, io);
    case "import":
      return importFile(p, io);
    case "repair":
      return repair(p, io);
    case "delete":
      return remove(p, io);
    case "gc":
      return gc(p, io);
    case "eval":
      return evals(p.eval, io);
    default:
      io.err(USAGE);
      return 2;
  }
}

function isHost(value: unknown): value is Host {
  return (
    typeof value === "object" &&
    value !== null &&
    "fetch" in value &&
    typeof value.fetch === "function" &&
    "ready" in value &&
    typeof value.ready === "function" &&
    "channels" in value &&
    Array.isArray(value.channels)
  );
}

async function loadHost(
  module: string | undefined,
  io: Io,
): Promise<Host | undefined> {
  const path = resolve(module ?? "threads.config.ts");
  const loaded: unknown = await import(path);
  const found =
    typeof loaded === "object" && loaded !== null && "default" in loaded
      ? loaded.default
      : undefined;
  if (isHost(found)) return found;
  io.err(`${path} must export default host({...})`);
  return undefined;
}

/**
 * devSandbox()'s provider name. It runs commands on the host inside an OS confinement, with no
 * snapshots and nothing between the host and a leak of that confinement, so `start` refuses it:
 * it is for `dev` only.
 */
const DEV_PROVIDER = "dev";

const NOT_IN_PRODUCTION =
  "threads start: devSandbox() is for development only; use a provider sandbox in production, or run threads dev\n";

/** `threads dev` / `start`: host().ready() and host().fetch on a local server. */
async function serve(
  p: Parsed,
  io: Io,
  serving: ((served: Served) => void) | undefined,
): Promise<number> {
  const h = await loadHost(p.args[0], io);
  if (h === undefined) return 1;
  if (
    p.command === "start" &&
    hostSandboxes(h).some((s) => s.info.provider === DEV_PROVIDER)
  ) {
    io.err(NOT_IN_PRODUCTION);
    return 2;
  }
  await h.ready();
  const server = Bun.serve({ port: p.port, fetch: h.fetch });
  const base = `http://localhost:${server.port}`;
  io.out(`threads: ${p.command} listening on ${base}\n`);
  for (const name of h.channels)
    io.out(`  ${name}: ${base}/channels/${encodeURIComponent(name)}/events\n`);
  const stop = async (): Promise<void> => {
    await h.stop();
    await server.stop();
  };
  if (serving !== undefined) serving({ url: base, stop });
  else
    process.once("SIGINT", () => {
      void stop();
    });
  return 0;
}

/** The store `--store` names: a directory (SQLite), or a postgres:// URL. */
async function storeAt(at: string): Promise<Store> {
  if (!/^postgres(ql)?:\/\//.test(at)) return sqlite(at);
  // Loaded only for a Postgres store: a SQLite user never loads pg.
  const { postgres } = await import("@threads/postgres");
  return postgres(at);
}

async function logOf(p: Parsed) {
  const { log } = await openStore(
    tenantStore(await storeAt(p.store), p.tenant ?? "local"),
  );
  return log;
}

async function timeline(p: Parsed, io: Io): Promise<number> {
  const id = ThreadId.safeParse(p.args[0]);
  const branch =
    p.branch === undefined ? undefined : BranchId.safeParse(p.branch);
  if (!id.success || branch?.success === false)
    return usage(io, "timeline <thread_id>");
  const store = tenantStore(await storeAt(p.store), p.tenant ?? "local");
  const thread = await openThread(store, id.data, {
    ...(branch === undefined ? {} : { branchId: branch.data }),
  });
  if (!thread.ok) return fail(io, thread.error);
  const steps = await thread.value.timeline();
  if (!steps.ok) return fail(io, steps.error);
  for (const entry of steps.value.entries) io.out(`${JSON.stringify(entry)}\n`);
  return 0;
}

async function exportBranch(p: Parsed, io: Io): Promise<number> {
  const id = BranchId.safeParse(p.args[0]);
  if (!id.success) return usage(io, "export <branch_id> [--bundle <dir>]");
  if (p.bundle !== undefined) return await exportBundleTo(p, io, id.data);
  const bytes = await (await logOf(p)).exportBranch(id.data);
  if (!bytes.ok) return fail(io, bytes.error);
  io.bytes(bytes.value);
  return 0;
}

/** `--bundle`: thread.export, the public function, and nothing else. */
async function exportBundleTo(
  p: Parsed,
  io: Io,
  branchId: BranchId,
): Promise<number> {
  const store = tenantStore(await storeAt(p.store), p.tenant ?? "local");
  const { log } = await openStore(store);
  const read = await log.read(branchId);
  if (!read.ok) return fail(io, read.error);
  const threadId = read.value.segments[0]?.header.thread_id;
  if (threadId === undefined)
    return fail(io, {
      code: "branch_not_found",
      message: `no branch ${branchId}`,
    });
  const thread = await openThread(store, threadId, { branchId });
  if (!thread.ok) return fail(io, thread.error);
  const written = await thread.value.export(p.bundle ?? "");
  if (!written.ok) return fail(io, written.error);
  io.out(`${written.value.path}\n`);
  return 0;
}

async function importFile(p: Parsed, io: Io): Promise<number> {
  const path = p.args[0];
  if (path === undefined) return usage(io, "import <dir|file>");
  const store = tenantStore(await storeAt(p.store), p.tenant ?? "local");
  const stored = await importThread(store, path);
  if (!stored.ok) return fail(io, stored.error);
  io.out(`${stored.value.branch}\n`);
  return 0;
}

async function repair(p: Parsed, io: Io): Promise<number> {
  const id = BranchId.safeParse(p.args[0]);
  if (!id.success) return usage(io, "repair <branch_id>");
  const writer = await (await logOf(p)).acquire(
    id.data,
    `cli-${crypto.randomUUID()}`,
  );
  if (!writer.ok) return fail(io, writer.error);
  await writer.value.release();
  io.out(`${id.data} is runnable\n`);
  return 0;
}

async function remove(p: Parsed, io: Io): Promise<number> {
  const { db } = await storeConnection(await storeAt(p.store));
  const thread = p.args[0];
  if (thread === undefined && p.tenant !== undefined) {
    const done = await deleteTenant(db, p.tenant, Date.now());
    if (!done.ok) return fail(io, done.error);
    io.out(`deleted ${done.value} threads of ${p.tenant}\n`);
    return 0;
  }
  if (thread === undefined)
    return usage(io, "delete <thread_id> | --tenant <id>");
  const id = ThreadId.safeParse(thread);
  if (!id.success)
    return fail(io, { code: "not_found", message: `no thread ${thread}` });
  const done = await deleteThread(db, p.tenant ?? "local", id.data, Date.now());
  if (!done.ok) return fail(io, done.error);
  io.out(`deleted ${thread} (${done.value} threads)\n`);
  return 0;
}

async function gc(p: Parsed, io: Io): Promise<number> {
  const store = await storeAt(p.store);
  const { db } = await storeConnection(store);
  const { artifacts } = await openStore(store);
  if (p.args[0] !== undefined) {
    const h = await loadHost(p.args[0], io);
    if (h === undefined) return 1;
    const log = await logOf(p);
    for (const sandbox of hostSandboxes(h)) {
      const done = await collect(log.ledger, sandbox);
      if (!done.ok) return fail(io, done.error);
      io.out(
        `released ${done.value.length} ${sandbox.info.provider} resources\n`,
      );
    }
  }
  const grace = p.graceDays * 86_400_000;
  const removed = await sweepArtifacts(db, artifacts, Date.now() - grace);
  io.out(`removed ${removed.length} unreferenced artifacts\n`);
  return 0;
}

function usage(io: Io, text: string): number {
  io.err(`usage: threads ${text}\n`);
  return 2;
}

function fail(
  io: Io,
  error: { readonly code: string; readonly message: string },
): number {
  io.err(`${error.code}: ${error.message}\n`);
  return 1;
}

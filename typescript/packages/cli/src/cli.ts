import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { openThread, sqlite } from "@threads/core";
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
  export <branch_id>                write a branch's JSONL export to stdout
  import <file>                     verify and store an export
  repair <branch_id>                record log_repaired on an imported torn branch
  delete <thread_id> | --tenant T   delete a thread, or every thread of a tenant
  gc [module] [--grace-days N]      release collectable resources, sweep unreferenced artifacts
options: --store <dir> (default .threads), --tenant <id> (default local)`;

const OPTIONS = {
  store: { type: "string", default: ".threads" },
  tenant: { type: "string" },
  branch: { type: "string" },
  port: { type: "string", default: "8787" },
  "grace-days": { type: "string", default: "7" },
} as const;

type Parsed = {
  readonly command: string | undefined;
  readonly args: readonly string[];
  readonly store: string;
  readonly tenant: string | undefined;
  readonly branch: string | undefined;
  readonly port: number;
  readonly graceDays: number;
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
    store: values.store,
    tenant: values.tenant,
    branch: values.branch,
    port: Number(values.port),
    graceDays: Number(values["grace-days"]),
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

/** `threads dev` / `start`: host().ready() and host().fetch on a local server. */
async function serve(
  p: Parsed,
  io: Io,
  serving: ((served: Served) => void) | undefined,
): Promise<number> {
  const h = await loadHost(p.args[0], io);
  if (h === undefined) return 1;
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

async function logOf(p: Parsed) {
  const { log } = await openStore(
    tenantStore(sqlite(p.store), p.tenant ?? "local"),
  );
  return log;
}

async function timeline(p: Parsed, io: Io): Promise<number> {
  const id = ThreadId.safeParse(p.args[0]);
  const branch =
    p.branch === undefined ? undefined : BranchId.safeParse(p.branch);
  if (!id.success || branch?.success === false)
    return usage(io, "timeline <thread_id>");
  const store = tenantStore(sqlite(p.store), p.tenant ?? "local");
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
  if (!id.success) return usage(io, "export <branch_id>");
  const bytes = (await logOf(p)).exportBranch(id.data);
  if (!bytes.ok) return fail(io, bytes.error);
  io.bytes(bytes.value);
  return 0;
}

async function importFile(p: Parsed, io: Io): Promise<number> {
  const file = p.args[0];
  if (file === undefined) return usage(io, "import <file>");
  const stored = (await logOf(p)).importLog(new Uint8Array(readFileSync(file)));
  if (!stored.ok) return fail(io, stored.error);
  const leaf = stored.value.segments.at(-1)?.header.branch_id;
  io.out(`${leaf ?? ""}\n`);
  return 0;
}

async function repair(p: Parsed, io: Io): Promise<number> {
  const id = BranchId.safeParse(p.args[0]);
  if (!id.success) return usage(io, "repair <branch_id>");
  const writer = (await logOf(p)).acquire(
    id.data,
    `cli-${crypto.randomUUID()}`,
  );
  if (!writer.ok) return fail(io, writer.error);
  writer.value.release();
  io.out(`${id.data} is runnable\n`);
  return 0;
}

async function remove(p: Parsed, io: Io): Promise<number> {
  const { db } = await storeConnection(sqlite(p.store));
  const thread = p.args[0];
  if (thread === undefined && p.tenant !== undefined) {
    const done = deleteTenant(db, p.tenant, Date.now());
    if (!done.ok) return fail(io, done.error);
    io.out(`deleted ${done.value} threads of ${p.tenant}\n`);
    return 0;
  }
  if (thread === undefined)
    return usage(io, "delete <thread_id> | --tenant <id>");
  const id = ThreadId.safeParse(thread);
  if (!id.success)
    return fail(io, { code: "not_found", message: `no thread ${thread}` });
  const done = deleteThread(db, p.tenant ?? "local", id.data, Date.now());
  if (!done.ok) return fail(io, done.error);
  io.out(`deleted ${thread} (${done.value} threads)\n`);
  return 0;
}

async function gc(p: Parsed, io: Io): Promise<number> {
  const store = sqlite(p.store);
  const { db } = await storeConnection(store);
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
  const removed = sweepArtifacts(
    db,
    join(p.store, "artifacts"),
    Date.now() - grace,
  );
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

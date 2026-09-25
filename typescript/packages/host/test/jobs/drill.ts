import {
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LogStore, memoryArtifacts } from "@threads/core";
import {
  type KnownEvent,
  knownEvents,
  ThreadId,
  type VerifiedLog,
} from "@threads/core/host";
import { z } from "zod";
import { sqlAll, sqlRun } from "../sql";
import { drillDriver } from "./stores";
import { rows, TEAM } from "./worker";

// The parent side of the crash drills: spawns worker.ts processes on one store, stops or kills
// them at scripted points, and reads the outcome back from the log and the fake provider's files.

const WORKER = join(import.meta.dir, "worker.ts");
const WAIT_MS = 20_000;

export type Worker = {
  readonly proc: Bun.Subprocess<"ignore", "pipe", Bun.BunFile>;
  readonly lines: AsyncIterator<string>;
};

const spawned: Worker[] = [];
const dirs: string[] = [];

export function scratch(): string {
  const dir = mkdtempSync(join(tmpdir(), "threads-drill-"));
  dirs.push(dir);
  return dir;
}

export function spawn(
  role: string,
  where: string,
  env: Record<string, string> = {},
  script: string = WORKER,
): Worker {
  const proc = Bun.spawn([process.execPath, script, role, where], {
    env: { ...process.env, ...env },
    stdout: "pipe",
    // Kept per worker so a drill can assert its hosts logged no failure.
    stderr: Bun.file(join(where, `stderr-${spawned.length}.log`)),
  });
  const worker = { proc, lines: lines(proc.stdout) };
  spawned.push(worker);
  return worker;
}

async function* lines(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<string> {
  const decoder = new TextDecoder();
  let buffered = "";
  for await (const chunk of stream) {
    buffered += decoder.decode(chunk, { stream: true });
    for (
      let at = buffered.indexOf("\n");
      at >= 0;
      at = buffered.indexOf("\n")
    ) {
      yield buffered.slice(0, at);
      buffered = buffered.slice(at + 1);
    }
  }
}

/** After every drill, passed or failed: no worker outlives it, and its store is removed. */
export async function reap(): Promise<void> {
  for (const w of spawned.splice(0)) {
    w.proc.kill(9);
    await w.proc.exited;
  }
  for (const dir of dirs.splice(0))
    rmSync(dir, { recursive: true, force: true });
}

async function within<T>(promise: Promise<T>, what: string): Promise<T> {
  const timer = Promise.withResolvers<never>();
  const t = setTimeout(
    () => timer.reject(new Error(`timed out: ${what}`)),
    WAIT_MS,
  );
  try {
    return await Promise.race([promise, timer.promise]);
  } finally {
    clearTimeout(t);
  }
}

/** Until the worker reports it is blocked at `point`. */
export async function waitAt(w: Worker, point: string): Promise<void> {
  const next = await within(w.lines.next(), `the worker reaching ${point}`);
  if (next.done === true || next.value !== `at ${point}`)
    throw new Error(`the worker ended before ${point}: ${String(next.value)}`);
}

export async function kill(w: Worker): Promise<void> {
  w.proc.kill(9);
  await within(w.proc.exited, "the killed worker exiting");
  if (w.proc.signalCode !== "SIGKILL") throw new Error("not killed");
}

export async function finish(w: Worker): Promise<number> {
  return within(w.proc.exited, "the worker finishing");
}

export function release(where: string): void {
  writeFileSync(join(where, "release"), "");
}

export function go(where: string): void {
  writeFileSync(join(where, "go"), "");
}

/** A killed holder's lease runs out after its TTL (30 s); the drill moves its expiry to now. */
export async function expireLeases(where: string): Promise<void> {
  const db = (await drillDriver(where)).db;
  try {
    await sqlRun(db, "UPDATE leases SET expires_at = 0", []);
  } finally {
    await db.close();
  }
}

/** The drill conversation's main branch, read on a connection closed right after. */
export async function log(where: string): Promise<VerifiedLog | undefined> {
  const db = (await drillDriver(where)).db;
  try {
    const opened = await LogStore.open(db, Date.now, memoryArtifacts(), TEAM);
    if (!opened.ok) throw new Error(opened.error.message);
    const [first] = z
      .array(z.strictObject({ thread_id: ThreadId }))
      .parse(await sqlAll(db, "SELECT thread_id FROM inbox LIMIT 1", []));
    if (first === undefined) return undefined;
    const main = await opened.value.mainBranch(first.thread_id);
    const read = main.ok ? await opened.value.read(main.value) : undefined;
    return read?.ok === true ? read.value : undefined;
  } finally {
    await db.close();
  }
}

export async function events(where: string): Promise<readonly KnownEvent[]> {
  const read = await log(where);
  return read === undefined ? [] : knownEvents(read);
}

/** What the drill's workers wrote to stderr: a host logs every failure it survives there. */
export function logged(where: string): string {
  return readdirSync(where)
    .filter((f) => f.startsWith("stderr-"))
    .map((f) => readFileSync(join(where, f), "utf8"))
    .join("");
}

export function sends(where: string): readonly string[] {
  return rows(where, "sends.jsonl").map((r) => String(r["key"]));
}

/** No interleaved appends: contiguous seqs and non-decreasing epochs. */
export function oneWriterAtATime(all: readonly KnownEvent[]): void {
  const seqs = all.map((e) => e.seq);
  if (seqs.some((s, i) => s !== i + 1)) throw new Error(`seqs ${seqs}`);
  const epochs = all.map((e) => e.epoch);
  if (epochs.some((e, i) => i > 0 && e < (epochs[i - 1] ?? 0)))
    throw new Error(`epochs ${epochs}`);
}

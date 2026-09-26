import type { Sinks } from "@threads/core/adapter";
import { z } from "zod";
import { type Engine, failure } from "./engine";
import { startExec } from "./exec";
import { untar } from "./tar";
import { parsed, unavailable } from "./wire";

// D-2: what `terminate(K)` answers, and how. The records the supervisor writes are read with
// one archive GET, never a probe, so a stopped container and an exhausted pid table both
// still answer. Only when K is live in the current generation does the in-container probe
// run, and its exit status is the contract (lane 16 D1).

type SupervisorRecord = {
  readonly key: string;
  readonly generation: string;
  readonly state: "running" | "exited" | "terminated" | "stuck";
  readonly child_pid: number;
  readonly supervisor_pid: number;
  readonly supervisor_start: number;
  readonly deadline_ms: number;
  readonly exit_code: number;
};

const RecordWire: z.ZodType<SupervisorRecord> = z.object({
  key: z.string().min(1),
  generation: z.string(),
  state: z.enum(["running", "exited", "terminated", "stuck"]),
  child_pid: z.int(),
  supervisor_pid: z.int(),
  supervisor_start: z.int(),
  deadline_ms: z.int(),
  exit_code: z.int(),
});

/** The probe's exit statuses (docker/supervise/supervise.h). */
const KILLED = 10;
const SIGNALLED = 11;
const GONE = 12;
const BUSY = 13;

/** How long the host waits for a container or a record to settle, and how often it looks. */
const SETTLE_MS = 10_000;
const POLL_MS = 25;
/** Probe rounds before the answer is unknown: each must see a changed record to go on. */
const ROUNDS = 3;

export type Answer = "terminated" | "already_exited" | "unknown";

export type State = {
  readonly generation: string;
  readonly records: ReadonlyMap<string, SupervisorRecord>;
};

/** `state/generation` and every `state/records/*.json`, in one archive GET. */
export async function readState(
  engine: Engine,
  name: string,
): Promise<State | undefined> {
  const res = await engine.send(
    "GET",
    `/containers/${encodeURIComponent(name)}/archive?path=${encodeURIComponent("/run/threads/state")}`,
  );
  if (res.status === 404) {
    await res.text();
    return undefined;
  }
  if (!res.ok) throw await failure(res, "the state archive");
  const text = new TextDecoder();
  const records = new Map<string, SupervisorRecord>();
  let generation = "";
  for (const file of untar(new Uint8Array(await res.arrayBuffer()))) {
    if (file.name === "state/generation")
      generation = text.decode(file.bytes).trim();
    const key = /^state\/records\/([0-9a-f]{32})\.json$/.exec(file.name)?.[1];
    if (key !== undefined)
      records.set(key, parsed(RecordWire, file.bytes, `the record of ${key}`));
  }
  return { generation, records };
}

/**
 * What a record alone settles. A running or stuck record of an older generation reads as
 * terminated: the container stopped, so every process in it died. undefined: K may be live.
 */
function settled(state: State, key: string): Answer | undefined {
  const record = state.records.get(key);
  if (record === undefined) return "unknown";
  if (record.state === "exited") return "already_exited";
  if (record.state === "terminated") return "terminated";
  return record.generation === state.generation ? undefined : "terminated";
}

const sleep = (ms: number) => new Promise((done) => setTimeout(done, ms));

/** Polls `look` until it answers, or until `withinMs` has passed. */
async function until<T>(
  withinMs: number,
  look: () => Promise<T | undefined>,
): Promise<T | undefined> {
  const deadline = Date.now() + withinMs;
  for (;;) {
    const answer = await look();
    if (answer !== undefined) return answer;
    if (Date.now() >= deadline) return undefined;
    await sleep(POLL_MS);
  }
}

/** Runs `supervise --terminate <key>` as uid 0 and returns its exit status. */
async function probe(
  engine: Engine,
  name: string,
  key: string,
): Promise<number> {
  const sinks: Sinks = { stdout: () => undefined, stderr: () => undefined };
  const started = await startExec(
    engine,
    name,
    { user: "0", cmd: ["/run/threads/bin/supervise", "--terminate", key] },
    sinks,
    // Killing --idle kills the pid namespace, the probe included, so the exec often ends
    // with no code at all. That is row 5 either way.
    () => KILLED,
  );
  return started.exit;
}

async function stopped(engine: Engine, name: string): Promise<Answer> {
  const answer = await until(SETTLE_MS, async () => {
    const seen = await engine.inspect(name);
    return seen === undefined || !seen.running
      ? ("terminated" as const)
      : undefined;
  });
  return answer ?? "unknown";
}

/** Waits for K's record to leave `running`, then answers from it. */
async function leftRunning(
  engine: Engine,
  name: string,
  key: string,
  withinMs: number,
): Promise<Answer | "stuck"> {
  const answer = await until(withinMs, async () => {
    const state = await readState(engine, name);
    if (state === undefined) return "terminated" as const;
    if (state.records.get(key)?.state === "stuck") return "stuck" as const;
    return settled(state, key);
  });
  return answer ?? "unknown";
}

/**
 * terminate(K), in D-2's order. Every row answers from a record or from a control-plane fact
 * (the container is stopped); nothing is inferred from inside the guest.
 */
/**
 * One probe's status as D-2 reads it, or "again": re-read the record and probe once more.
 * 0 is K not live in this generation, 12 is K's supervisor gone; both are settled by the
 * re-read. Anything else is a probe that failed, and the answer is unknown.
 */
async function fromProbe(
  engine: Engine,
  name: string,
  key: string,
  status: number,
  deadlineMs: number,
): Promise<Answer | "again"> {
  if (status === KILLED) return stopped(engine, name);
  if (status === SIGNALLED) {
    const left = await leftRunning(
      engine,
      name,
      key,
      Math.min(SETTLE_MS, deadlineMs + 5_000),
    );
    return left === "stuck" ? "again" : left;
  }
  if (status === BUSY) {
    const left = await leftRunning(engine, name, key, deadlineMs + 5_000);
    // A stuck record blocks admission until the container restarts, so it goes back to the
    // probe (which then takes the lock and stops the container), never to unknown.
    return left === "stuck" ? "again" : left;
  }
  return status === 0 || status === GONE ? "again" : "unknown";
}

export async function terminate(
  engine: Engine,
  name: string,
  key: string,
): Promise<Answer> {
  const container = await engine.inspect(name);
  if (container === undefined) return "unknown";
  if (!container.running) return "terminated";
  for (let round = 0; round < ROUNDS; round += 1) {
    const state = await readState(engine, name);
    if (state === undefined) return "unknown";
    const answer = settled(state, key);
    if (answer !== undefined) return answer;
    const next = await fromProbe(
      engine,
      name,
      key,
      await probe(engine, name, key),
      state.records.get(key)?.deadline_ms ?? SETTLE_MS,
    );
    if (next !== "again") return next;
  }
  return "unknown";
}

/**
 * What a supervised command exited with. `supervise` always exits 0 itself once it has
 * recorded the command, so the code is the record's, never the exec's; the exec's status only
 * says whether the supervisor got that far. An admission refusal ran nothing at all, so it is
 * a failure and not in doubt.
 */
export async function commandExit(
  engine: Engine,
  name: string,
  key: string,
  status: number,
  stderr: string,
): Promise<number> {
  if (status === 125 || stderr.includes("threads: admission refused"))
    throw unavailable("another command is still running in this sandbox");
  const record = (await readState(engine, name))?.records.get(key);
  if (record?.state === "exited") return record.exit_code;
  // The sweep proved every process of the group was killed.
  if (record?.state === "terminated") return 137;
  if (record?.state === "stuck")
    throw unavailable(
      `a process of the command in ${name} could not be killed`,
    );
  throw unavailable(
    status === 0
      ? `the command left no final record in ${name}`
      : `the supervisor exited ${status}: ${stderr.trim()}`,
  );
}

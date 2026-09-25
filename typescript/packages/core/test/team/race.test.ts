import { Database } from "bun:sqlite";
import { afterAll, describe, expect, test } from "bun:test";
import { copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import { BranchId, type KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { fixture, unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import type { Op } from "./op-run";
import { DOC, seeded, TEAM, type Vector, worldLogs } from "./vectors";

// The two-process races design §7 names, each over many schedules: two processes, each with its
// own connection to one store file and its own clock, run one op each at the same moment, and
// SQLite serializes their transactions in either order. Every assertion reads log evidence only; no
// pre-commit order and no created_at is used.

// The design's 1,000 schedules are the jobs run: THREADS_RACE_SCHEDULES=1000 bun test race.
const SCHEDULES = Number(process.env["THREADS_RACE_SCHEDULES"] ?? 100);
const dir = mkdtempSync(join(tmpdir(), "threads-race-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

/** A long-lived child process: one job line in, one answer line out. */
function child() {
  const proc = Bun.spawn(["bun", join(import.meta.dir, "race-worker.ts")], {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "inherit",
  });
  const lines = proc.stdout.pipeThrough(new TextDecoderStream()).getReader();
  let buffered = "";
  const next = async (): Promise<string> => {
    for (;;) {
      const at = buffered.indexOf("\n");
      if (at >= 0) {
        const line = buffered.slice(0, at);
        buffered = buffered.slice(at + 1);
        return line;
      }
      const { value, done } = await lines.read();
      if (done) throw new Error("a race process exited");
      buffered += value;
    }
  };
  return {
    ask: async (job: unknown): Promise<unknown> => {
      proc.stdin.write(`${JSON.stringify(job)}\n`);
      proc.stdin.flush();
      return JSON.parse(await next());
    },
    kill: () => proc.kill(),
  };
}

const processes = [child(), child()];
afterAll(() => {
  for (const p of processes) p.kill();
});

type Side = { readonly label: string; readonly op: Op; readonly now: number };

const vector = (name: string): Vector => {
  const v = DOC.vectors.find((x) => x.name === name);
  if (v === undefined) throw new Error(`no vector ${name}`);
  return v;
};

function runSide(
  proc: ReturnType<typeof child>,
  path: string,
  world: Vector,
  side: Side,
): Promise<unknown> {
  return proc.ask({
    path,
    branch: worldLogs(world)[side.label]?.branch_id,
    now: side.now,
    spinMs: Math.random() * 2,
    op: side.op,
  });
}

const templates = new Map<string, string>();

/** The world seeded once into a store file; each schedule starts from a copy. */
async function template(world: Vector): Promise<string> {
  const known = templates.get(world.name);
  if (known !== undefined) return known;
  const path = join(dir, `${world.name}.db`);
  const db = openBunSqlite(path);
  await seeded(world, db);
  await db.close();
  // One file: the copy must not need the write-ahead log beside it (the store's driver runs every
  // statement in a transaction, where a checkpoint can't truncate).
  const raw = new Database(path);
  raw.exec("PRAGMA wal_checkpoint(TRUNCATE)");
  raw.close();
  templates.set(world.name, path);
  return path;
}

/** One schedule: the world in a fresh file, both sides at once; the logs after, by label. */
async function schedule(n: number, world: Vector, a: Side, b: Side) {
  const path = join(dir, `${world.name}-${n}.db`);
  copyFileSync(await template(world), path);
  const fx = await fixture("acme", openBunSqlite(path));
  const [wa, wb] = processes;
  if (wa === undefined || wb === undefined) throw new Error("two processes");
  await Promise.all([runSide(wa, path, world, a), runSide(wb, path, world, b)]);
  const logs = new Map<string, readonly KnownEvent[]>();
  for (const [label, ref] of Object.entries(worldLogs(world)))
    logs.set(
      label,
      knownEvents(unwrap(await fx.store.read(BranchId.parse(ref.branch_id)))),
    );
  return { fx, logs };
}

const sent = (log: readonly KnownEvent[] | undefined, kind: string) =>
  (log ?? []).flatMap((e) =>
    e.type === "message_sent" && e.data.envelope.kind === kind
      ? [e.data.envelope]
      : [],
  );

describe("two-process races", () => {
  test(`deadline race: a settlement counts iff it deleted the monitor row first (${SCHEDULES} schedules)`, async () => {
    const world = vector("idle-fires-settle-and-task-monitors");
    const waitId = "0192b000-0000-7000-8000-0000000000b1:c3";
    const due = 1_790_000_220_000;
    const seen = new Set<boolean>();
    for (let n = 0; n < SCHEDULES; n += 1) {
      const { fx, logs } = await schedule(
        n,
        world,
        {
          label: "lead",
          op: { ...world, op: "deadline", input: { id: waitId } },
          now: due,
        },
        {
          label: "researcher",
          op: { ...world, op: "idle", input: {} },
          now: due,
        },
      );
      const lead = logs.get("lead") ?? [];
      const started = lead.find((e) => e.type === "wait_started");
      const monitor = `${started?.branch_id}:${started?.event_id}:researcher-1`;
      const fired = sent(logs.get("researcher"), "member_settled").some(
        (m) => m.monitor_id === monitor,
      );
      const finished = lead.flatMap((e) =>
        e.type === "wait_finished" && e.data.wait_id === waitId ? [e] : [],
      );
      expect(finished).toHaveLength(1);
      const data = finished[0]?.data;
      // finished is exactly the members whose firing won the monitor row.
      expect(data?.finished.map((r): string => r.member.name)).toEqual(
        fired ? ["researcher-1"] : [],
      );
      expect(data?.timed_out).toBe(!fired);
      seen.add(fired);
      if (n % 50 === 0) await assertTeamReplays(fx.store, TEAM);
    }
    // Both orders happened.
    expect(seen.size).toBe(2);
  }, 600_000);

  test(`already-settled race: exactly one of observation or mail (${SCHEDULES} schedules)`, async () => {
    const world = vector("wait-registers-monitor");
    const ended = {
      reason: "error",
      result: {
        status: "failed",
        error: { code: "model_error", message: "stopped" },
      },
    };
    const seen = new Set<string>();
    for (let n = 0; n < SCHEDULES; n += 1) {
      const { fx, logs } = await schedule(
        n,
        world,
        { label: "lead", op: world, now: world.now },
        {
          label: "researcher",
          op: { ...world, op: "end", input: ended },
          now: world.now,
        },
      );
      const lead = logs.get("lead") ?? [];
      const started = lead.find((e) => e.type === "wait_started");
      const monitor = `${started?.branch_id}:${started?.event_id}:researcher-1`;
      const observed = lead.some(
        (e) => e.type === "member_observed" && e.data.monitor_id === monitor,
      );
      const mailed = sent(logs.get("researcher"), "member_ended").some(
        (m) => m.monitor_id === monitor,
      );
      expect(observed !== mailed).toBe(true);
      seen.add(observed ? "observed" : "mailed");
      if (n % 50 === 0) await assertTeamReplays(fx.store, TEAM);
    }
    expect(seen.size).toBe(2);
  }, 600_000);

  test(`reply versus deadline: answered iff the reply was sent (${SCHEDULES} schedules)`, async () => {
    const world = vector("reply-sent");
    const askId = z
      .string()
      .parse(
        z.object({ ask_id: z.string() }).parse(world.input["args"]).ask_id,
      );
    const due = 1_790_000_220_000;
    const seen = new Set<string>();
    for (let n = 0; n < SCHEDULES; n += 1) {
      const { fx, logs } = await schedule(
        n,
        world,
        // The reply's clock is just before the deadline or at it.
        { label: "researcher", op: world, now: due - (n % 3) },
        {
          label: "writer",
          op: { ...world, op: "deadline", input: { id: askId } },
          now: due,
        },
      );
      const replied = sent(logs.get("researcher"), "reply").length === 1;
      const closed = (logs.get("writer") ?? []).flatMap((e) =>
        e.type === "ask_closed" ? [e.data.outcome.status] : [],
      );
      expect(closed).toEqual([replied ? "answered" : "timed_out"]);
      seen.add(closed[0] ?? "");
      if (n % 50 === 0) await assertTeamReplays(fx.store, TEAM);
    }
    expect(seen.size).toBe(2);
  }, 600_000);

  test(`cancel versus the member's end: refused member_ended, or sent and refused by the end (${SCHEDULES} schedules)`, async () => {
    const world = vector("cancel-requested");
    const ended = {
      reason: "error",
      result: {
        status: "failed",
        error: { code: "model_error", message: "stopped" },
      },
    };
    const seen = new Set<string>();
    for (let n = 0; n < SCHEDULES; n += 1) {
      const { fx, logs } = await schedule(
        n,
        world,
        { label: "lead", op: world, now: world.now },
        {
          label: "researcher",
          op: { ...world, op: "end", input: ended },
          now: world.now,
        },
      );
      const lead = logs.get("lead") ?? [];
      const cancels = sent(lead, "cancel");
      const refused = lead.some(
        (e) =>
          e.type === "tool_result" &&
          e.data.preview.includes('"code":"member_ended"'),
      );
      const bounced = (logs.get("researcher") ?? []).some(
        (e) =>
          e.type === "mail_refused" && e.data.mail_id === cancels[0]?.mail_id,
      );
      // The cancel was sent iff the member was not ended yet, and then its end refuses it.
      expect(cancels.length === 1).toBe(!refused);
      expect(bounced).toBe(!refused);
      seen.add(refused ? "refused" : "sent");
      if (n % 50 === 0) await assertTeamReplays(fx.store, TEAM);
    }
    expect(seen.size).toBe(2);
  }, 600_000);
});

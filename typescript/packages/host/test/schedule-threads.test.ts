import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { agent, type Store, scriptedModel, sqlite } from "@threads/core";
import {
  type BranchId,
  deleteThread,
  openStore,
  storeConnection,
  ThreadId,
  tenantStore,
  type Writer,
} from "@threads/core/host";
import { z } from "zod";
import { HostContext } from "../src/context";
import { bindSchedules, type Schedule, tick } from "../src/schedules";
import { eventsOf, say } from "./kit";

// One thread per schedule, replayed from the shared vector
// spec/conformance/vectors/schedule-threads.json (the Python host suite replays the same file).

const ScheduleJson = z.strictObject({
  id: z.string(),
  agent: z.string(),
  cron: z.string(),
  input: z.string(),
});
const Expected = z.strictObject({
  log: z.array(z.string()),
  rows: z.array(z.tuple([z.string(), z.string(), z.string().nullable()])),
  threads: z.int().optional(),
  inputs: z.array(z.string()).optional(),
});
const Step = z.union([
  z.strictObject({
    start: z.string(),
    at: z.string(),
    tenant: z.string().optional(),
    schedules: z.array(ScheduleJson).optional(),
    agents: z.array(z.string()).optional(),
  }),
  z.strictObject({ tick: z.string(), at: z.string() }),
  z.strictObject({ race: z.array(z.string()), at: z.string() }),
  z.strictObject({ hold: z.enum(["run", "outbound"]) }),
  z.strictObject({ release: z.enum(["done", "crash"]) }),
  z.strictObject({ delete: z.literal(true) }),
  z.strictObject({ expect: z.record(z.string(), Expected) }),
]);
const Vector = z.strictObject({
  description: z.string(),
  schedule: ScheduleJson,
  cases: z.array(
    z.strictObject({
      name: z.string(),
      note: z.string(),
      steps: z.array(Step),
    }),
  ),
});
type Step = z.infer<typeof Step>;

const vector = Vector.parse(
  JSON.parse(
    readFileSync(
      join(
        import.meta.dir,
        "../../../../spec/conformance/vectors/schedule-threads.json",
      ),
      "utf8",
    ),
  ),
);

const OPERATOR = { issuer: "api", tenant: "local", subject: "operator" };

type Scheduler = {
  readonly ctx: HostContext;
  readonly bound: Exclude<ReturnType<typeof bindSchedules>, string>;
  readonly startedAt: number;
  readonly tenant: string;
};

/** One case's store, schedulers and outside writer. */
class Replay {
  readonly store: Store = sqlite(":memory:");
  readonly schedulers = new Map<string, Scheduler>();
  last: Scheduler | undefined;
  held:
    | { readonly writer: Writer; readonly kind: "run" | "outbound" }
    | undefined;

  async step(step: Step): Promise<void> {
    if ("start" in step) return this.start(step);
    if ("tick" in step) return this.tick([step.tick], step.at);
    if ("race" in step) return this.tick(step.race, step.at);
    if ("hold" in step) return this.hold(step.hold);
    if ("release" in step) return this.release(step.release);
    if ("delete" in step) return this.delete();
    for (const [tenant, want] of Object.entries(step.expect))
      expect(await this.observe(tenant, want)).toEqual(want);
  }

  start(step: Extract<Step, { start: string }>): void {
    const agents = step.agents ?? ["bot"];
    const ctx = new HostContext(
      this.store,
      Object.fromEntries(
        agents.map((key) => [
          key,
          agent({
            name: key,
            model: scriptedModel({ responses: Array(12).fill(say("ok")) }),
          }),
        ]),
      ),
      {},
    );
    const schedules: readonly Schedule[] = step.schedules ?? [vector.schedule];
    const bound = bindSchedules(ctx, schedules);
    if (typeof bound === "string") throw new Error(bound);
    const tenant = step.tenant ?? "local";
    const made = { ctx, bound, startedAt: Date.parse(step.at), tenant };
    this.schedulers.set(step.start, made);
    this.last = made;
  }

  async tick(names: readonly string[], at: string): Promise<void> {
    const ticking = names.map((name) => this.scheduler(name));
    await Promise.all(
      ticking.map((s) =>
        tick(s.ctx, s.bound, s.startedAt, Date.parse(at), s.tenant),
      ),
    );
    await Promise.all(ticking.map((s) => s.ctx.idle()));
  }

  async hold(kind: "run" | "outbound"): Promise<void> {
    const { log } = await openStore(tenantStore(this.store, "local"));
    const branch = await this.branch("local");
    if (branch === undefined) throw new Error("no schedule thread to hold");
    const writer = log.acquire(branch, "outside");
    if (!writer.ok) throw new Error(writer.error.message);
    if (kind === "run") {
      const busy = writer.value.append([
        {
          type: "user_input",
          type_version: 1,
          critical: true,
          actor: { kind: "user", principal: OPERATOR },
          data: { source: "api", text: "Busy." },
        },
      ]);
      if (!busy.ok) throw new Error(busy.error.message);
    }
    this.held = { writer: writer.value, kind };
  }

  async release(how: "done" | "crash"): Promise<void> {
    const held = this.held;
    if (held === undefined) throw new Error("nothing is held");
    held.writer.release();
    this.held = undefined;
    const branch = await this.branch("local");
    const thread = await this.thread("local");
    const last = this.last;
    if (how === "crash" || held.kind === "outbound") return;
    if (branch === undefined || thread === undefined || last === undefined)
      throw new Error("no run to finish");
    await last.ctx.recover("local", { id: thread, branch });
    await last.ctx.idle();
  }

  async delete(): Promise<void> {
    const { db } = await storeConnection(this.store);
    const thread = await this.thread("local");
    if (thread === undefined) throw new Error("no schedule thread to delete");
    const done = deleteThread(db, "local", thread, Date.now());
    if (!done.ok) throw new Error(done.error.message);
  }

  async observe(
    tenant: string,
    want: z.infer<typeof Expected>,
  ): Promise<z.infer<typeof Expected>> {
    const { db } = await storeConnection(this.store);
    const rows = z
      .array(
        z.strictObject({
          occurrence_at: z.int(),
          state: z.string(),
          reason: z.string().nullable(),
        }),
      )
      .parse(
        db.all(
          `SELECT occurrence_at, state, reason FROM schedule_occurrences WHERE tenant_id = ?
            ORDER BY occurrence_at`,
          [tenant],
        ),
      );
    const branch = await this.branch(tenant);
    const events =
      branch === undefined ? [] : await eventsOf(this.store, tenant, branch);
    // The schedule's thread opens with its one thread_started.
    if (events.length > 0) {
      expect(events[0]?.type).toBe("thread_started");
      expect(events.filter((e) => e.type === "thread_started")).toHaveLength(1);
    }
    const Occurrence = z.object({
      type: z.enum(["schedule_fired", "schedule_skipped"]),
      data: z.object({
        occurrence_id: z.string(),
        reason: z.string().optional(),
      }),
    });
    const Input = z.object({
      type: z.literal("user_input"),
      data: z.object({ source: z.literal("schedule"), text: z.string() }),
    });
    const [count] = z
      .array(z.strictObject({ n: z.int() }))
      .parse(
        db.all("SELECT count(*) AS n FROM threads WHERE tenant_id = ?", [
          tenant,
        ]),
      );
    return {
      log: events.flatMap((e) => {
        const o = Occurrence.safeParse(e);
        if (!o.success) return [];
        const { occurrence_id: id, reason } = o.data.data;
        return [
          o.data.type === "schedule_fired"
            ? `fired ${id}`
            : `skipped ${reason} ${id}`,
        ];
      }),
      rows: rows.map((r) => [
        new Date(r.occurrence_at).toISOString(),
        r.state,
        r.reason,
      ]),
      ...(want.threads === undefined ? {} : { threads: count?.n ?? 0 }),
      ...(want.inputs === undefined
        ? {}
        : {
            inputs: events.flatMap((e) => {
              const i = Input.safeParse(e);
              return i.success ? [i.data.data.text] : [];
            }),
          }),
    };
  }

  scheduler(name: string): Scheduler {
    const found = this.schedulers.get(name);
    if (found === undefined) throw new Error(`no scheduler ${name}`);
    return found;
  }

  async thread(tenant: string): Promise<ThreadId | undefined> {
    const { db } = await storeConnection(this.store);
    const [row] = z
      .array(z.strictObject({ thread_id: ThreadId }))
      .parse(
        db.all(
          "SELECT thread_id FROM schedule_threads WHERE tenant_id = ? AND schedule_id = ?",
          [tenant, vector.schedule.id],
        ),
      );
    return row?.thread_id;
  }

  async branch(tenant: string): Promise<BranchId | undefined> {
    const thread = await this.thread(tenant);
    if (thread === undefined) return undefined;
    const { log } = await openStore(tenantStore(this.store, tenant));
    const main = log.mainBranch(thread);
    return main.ok ? main.value : undefined;
  }

  async stop(): Promise<void> {
    for (const s of this.schedulers.values()) await s.ctx.stop();
  }
}

describe("schedule threads (spec/conformance/vectors/schedule-threads.json)", () => {
  for (const c of vector.cases)
    test(c.name, async () => {
      const replay = new Replay();
      try {
        for (const step of c.steps) await replay.step(step);
      } finally {
        await replay.stop();
      }
    }, 20_000);
});

import { z } from "zod";
import {
  BranchId,
  type MailEnvelope,
  type Principal,
  type TeamId,
  ThreadId,
} from "../../log";
import { knownEvents } from "../../reduce";
import { ok } from "../../result";
import { LEASE_TTL_MS, type LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import type { SqliteDriver } from "../../store/driver";
import { uuidv7 } from "../../store/encode";
import { getLease } from "../../store/tables";
import { isRefusal } from "../../store/writer";
import { Batch, type Mint } from "../../team/batch";
import { claimMail } from "../../team/claim";
import { TEAM_CONSTANTS } from "../../team/constants";
import { consumable, consume } from "../../team/consume";
import { materialize, type Rebind } from "../../team/materialize";
import { turnProvenance } from "../../team/provenance";
import { type RebindCode, rebindFailed } from "../../team/rebind";
import {
  type MemberRow,
  memberRows,
  ownRows,
  pendingFor,
  teamRow,
} from "../../team/rows";
import type { VerifiedLog } from "../../verify";
import { ConfigError } from "../errors";
import { memberEntry } from "../registry";
import type { Store } from "../sqlite";
import { ancestorsOf } from "./budgets";

// The in-process team worker (spec/schema/README.md, "Teams"; decision 17): while a lead's run
// is open, it materializes every starting member of the lead's team (and of each nested team),
// runs each member whose mail is pending, refuses mail that reaches an ended member, and resumes
// a member whose turn was left open with its lease free (hostless recovery). The lead consumes
// its own mail in its run. One member branch runs at a time; members run concurrently.

export type WorkerEnv = {
  readonly store: Store;
  readonly log: LogStore;
  readonly artifacts: ArtifactStore;
  readonly team: TeamId;
  /** Every agent of the lead's team tree, by name. */
  readonly agents: ReadonlyMap<string, object>;
  readonly signal?: AbortSignal;
  readonly mint?: Mint;
  /** How long a claim holds a pending row; tests inject a shorter one. */
  readonly claimTtlMs?: number;
};

const Row = z.object({ lead_thread_id: ThreadId, team_id: z.string() });

export class TeamWorker {
  readonly #env: WorkerEnv;
  /** Member runs in flight, by member thread. */
  readonly #running = new Map<string, Promise<void>>();
  #changed = Promise.withResolvers<void>();
  #failure: { readonly error: unknown } | undefined;
  #stopped = false;
  #loop: Promise<void> = Promise.resolve();
  readonly #token = `worker-${crypto.randomUUID()}`;

  constructor(env: WorkerEnv) {
    this.#env = env;
    // A failure is reported through progress() and stop(); nothing else awaits a rejection.
    this.#changed.promise.catch(() => undefined);
  }

  /** A team thread appended: look for work now, and wake whoever waits on progress. */
  readonly notify = (): void => {
    const changed = this.#changed;
    this.#changed = Promise.withResolvers<void>();
    this.#changed.promise.catch(() => undefined);
    if (this.#failure === undefined) changed.resolve();
    else changed.reject(this.#failure.error);
  };

  /** Settles on the team's next progress; rejects once a member run failed with a bug. */
  readonly progress = (): Promise<void> =>
    this.#failure === undefined
      ? this.#changed.promise
      : Promise.reject(this.#failure.error);

  /** A member run is in flight. */
  readonly busy = (): boolean => this.#running.size > 0;

  start(): void {
    this.#loop = this.#run();
  }

  /**
   * Stops looking for work and waits for the member runs in flight, unless the lead closed its
   * team: those members are being cancelled, and the run returns without them.
   */
  async stop(): Promise<void> {
    this.#stopped = true;
    this.notify();
    await this.#loop;
    if (!closed(this.#env.log.driver, this.#env.team))
      await Promise.allSettled(this.#running.values());
  }

  async #run(): Promise<void> {
    let recovering = true;
    while (!this.#stopped) {
      const changed = this.#changed.promise;
      this.#pass(recovering);
      recovering = false;
      const poll = Promise.withResolvers<void>();
      const timer = setTimeout(
        poll.resolve,
        TEAM_CONSTANTS.wakePollInProcessMs,
      );
      await Promise.race([changed.catch(() => undefined), poll.promise]);
      clearTimeout(timer);
    }
  }

  #pass(recovering: boolean): void {
    const db = this.#env.log.driver;
    for (const team of teamsUnder(db, this.#env.team))
      for (const row of memberRows(db, team)) {
        if (row.role !== "member" || this.#running.has(row.thread_id)) continue;
        const work = this.#work(db, row, recovering);
        if (work !== undefined) this.#launch(row.thread_id, work);
      }
  }

  /** What a member needs now, if anything. */
  #work(
    db: SqliteDriver,
    row: MemberRow,
    recovering: boolean,
  ): (() => Promise<void>) | undefined {
    // A closed team's members are being cancelled: none starts or resumes.
    if (row.state !== "ended" && closed(db, row.team_id)) return undefined;
    if (row.state === "starting") return () => this.#materialize(row);
    const branch = row.branch_id;
    if (branch === null) return undefined;
    // A parked member runs again at a run's start: what it waits on may be answered by now.
    if (row.state === "parked")
      return recovering ? () => this.#member(row, branch) : undefined;
    const pending = pendingFor(db, ownRows(db, row.thread_id));
    if (row.state === "ended")
      return pending.length > 0 ? () => this.#refuse(branch) : undefined;
    // A turn left open with its lease free is resumed (hostless recovery).
    const stranded =
      row.state === "running" && (recovering || this.#free(db, branch));
    const woken = this.#claim(db, pending.filter(consumable));
    return woken || stranded ? () => this.#member(row, branch) : undefined;
  }

  /**
   * mail.claim (design §4.6) on the first row this worker would wake the member for: a live claim
   * of another worker means that worker wakes it. Correctness never depends on it: the lease
   * holder consumes.
   */
  #claim(db: SqliteDriver, mail: readonly MailEnvelope[]): boolean {
    const first = mail[0];
    if (first === undefined) return false;
    const now = this.#env.log.now();
    const ttl = this.#env.claimTtlMs ?? TEAM_CONSTANTS.claimTtlMs;
    return claimMail(db, first.mail_id, this.#token, now, ttl) === "claimed";
  }

  #free(db: SqliteDriver, branch: string): boolean {
    const lease = getLease(db, branch);
    return lease.ok && (lease.value?.expires_at ?? 0) <= this.#env.log.now();
  }

  #launch(thread: string, work: () => Promise<void>): void {
    const running = (async () => {
      try {
        await work();
      } catch (error) {
        this.#failure ??= { error };
      } finally {
        this.#running.delete(thread);
        this.notify();
      }
    })();
    this.#running.set(thread, running);
  }

  async #materialize(row: MemberRow): Promise<void> {
    const holder = `team-${crypto.randomUUID()}`;
    const got = await materialize(this.#env.log, row.team_id, row.name, {
      artifacts: this.#env.artifacts,
      rebind: (agent, configHash) => this.#rebind(agent, configHash),
      holder,
      ttlMs: LEASE_TTL_MS,
      ...(this.#env.mint === undefined ? {} : { mint: this.#env.mint }),
    });
    if (!got.ok)
      throw new Error(`materialize ${row.name}: ${got.error.message}`);
    this.notify();
    if (got.value.status !== "materialized") return;
    const branch = got.value.writer.lease.branchId;
    await this.#member(row, branch, holder);
  }

  /** Rebinds a member's definition by name in this process (design §4.10, prework). */
  async #rebind(agent: string, configHash: string): Promise<Rebind> {
    const handle = this.#env.agents.get(agent);
    const entry = handle === undefined ? undefined : memberEntry(handle);
    if (entry === undefined) return { status: "pin_unavailable" };
    const pinned = await pinnedOrUnavailable(entry.pinned);
    if (pinned === undefined) return { status: "pin_unavailable" };
    if (pinned.configHash !== configHash) return { status: "pin_mismatch" };
    if (entry.team === undefined) return { status: "ok" };
    const now = this.#env.log.now();
    return {
      status: "ok",
      team: {
        id: uuidv7(now),
        log_thread_id: uuidv7(now),
        log_branch_id: uuidv7(now),
      },
    };
  }

  /** Runs a member branch until it is idle, parked or ended. */
  async #member(
    row: MemberRow,
    branch: string,
    holder = `team-${crypto.randomUUID()}`,
  ): Promise<void> {
    const read = this.#env.log.read(BranchId.parse(branch));
    if (!read.ok) throw new Error(`member ${row.name}: ${read.error.message}`);
    const events = knownEvents(read.value);
    const started = events.find((e) => e.type === "thread_started");
    const task = events.find((e) => e.type === "user_input");
    const parent =
      started?.type === "thread_started" ? started.data.parent : undefined;
    if (parent?.relation !== "team_member" || task?.type !== "user_input")
      throw new Error(`member ${row.name} has no task`);
    const handle = this.#env.agents.get(row.agent);
    const entry = handle === undefined ? undefined : memberEntry(handle);
    const rebind = await this.#rebind(
      row.agent,
      started?.data.config_hash ?? "",
    );
    if (entry === undefined || rebind.status !== "ok")
      return this.#unbound(
        branch,
        holder,
        rebind.status === "ok" ? "pin_unavailable" : rebind.status,
      );
    await entry.run({
      store: this.#env.store,
      thread: {
        id: row.thread_id,
        branch: BranchId.parse(branch),
        store: this.#env.store,
      },
      parent: { ...parent, relation: "team_member" },
      principal:
        principalOf(this.#env.log.driver, read.value, row) ??
        task.actor.principal,
      holder,
      notify: this.notify,
      covering: ancestorsOf(this.#env.log, parent),
      ...(this.#env.signal === undefined ? {} : { signal: this.#env.signal }),
    });
  }

  /** A member whose definition can't be rebound here ends failed, under its own writer. */
  #unbound(branch: string, holder: string, code: RebindCode): void {
    const writer = this.#env.log.acquire(BranchId.parse(branch), holder);
    // Held elsewhere: its holder runs it.
    if (!writer.ok) return;
    const w = writer.value;
    const header = w.chain.segments[0]?.header;
    if (header === undefined) throw new Error("a writer's chain has a header");
    const ended = w.appendDecided((tx) => {
      const batch = new Batch(tx.chain.fold.seq, tx.now, this.#env.mint);
      rebindFailed(
        {
          db: tx.db,
          chain: tx.chain,
          batch,
          threadId: header.thread_id,
          branchId: branch,
        },
        code,
        tx.now,
      );
      return ok(batch.drafts);
    });
    w.release();
    if (isRefusal(ended)) throw new Error("a failed rebind never refuses");
    if (!ended.ok) throw new Error(`member end: ${ended.error.message}`);
  }

  /** An ended member's writer refuses the mail that still reaches it. */
  async #refuse(branch: string): Promise<void> {
    const writer = this.#env.log.acquire(
      BranchId.parse(branch),
      `team-${crypto.randomUUID()}`,
    );
    if (!writer.ok) return;
    const w = writer.value;
    const header = w.chain.segments[0]?.header;
    if (header === undefined) throw new Error("a writer's chain has a header");
    w.appendDecided((tx) => {
      const batch = new Batch(tx.chain.fold.seq, tx.now, this.#env.mint);
      consume({
        db: tx.db,
        chain: tx.chain,
        batch,
        threadId: header.thread_id,
        branchId: branch,
      });
      return ok(batch.drafts);
    });
    w.release();
  }
}

/**
 * The one principal a member run acts under (design §2.6: one turn, one authority): its open
 * turn's, else that of the first mail it would take. Mail of another principal waits for the
 * next run.
 */
function principalOf(
  db: SqliteDriver,
  log: VerifiedLog,
  row: MemberRow,
): Principal | undefined {
  if (log.fold.turnOpen) return turnProvenance(db, log)?.principal;
  return pendingFor(db, ownRows(db, row.thread_id)).find(consumable)?.provenance
    .principal;
}

/** A definition that can't be set up or pinned here is unavailable: a value, not a throw. */
async function pinnedOrUnavailable<T>(
  pinned: () => Promise<T>,
): Promise<T | undefined> {
  try {
    return await pinned();
  } catch (error) {
    if (error instanceof ConfigError) return undefined;
    throw error;
  }
}

function closed(db: SqliteDriver, team: string): boolean {
  return (teamRow(db, team)?.closed_at ?? null) !== null;
}

/** The team and every team led by one of its members, recursively. */
function teamsUnder(db: SqliteDriver, root: TeamId): readonly string[] {
  const teams: string[] = [root];
  for (let i = 0; i < teams.length; i += 1) {
    const members = memberRows(db, teams[i] ?? "").map((r) => r.thread_id);
    const led = z
      .array(Row)
      .parse(db.all("SELECT lead_thread_id, team_id FROM teams", []))
      .filter(
        (t) => members.includes(t.lead_thread_id) && !teams.includes(t.team_id),
      );
    teams.push(...led.map((t) => t.team_id));
  }
  return teams;
}

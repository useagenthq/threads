import { BranchId, type MailEnvelope, type TeamId } from "../../log";
import { knownEvents } from "../../reduce";
import { LEASE_TTL_MS, type LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import type { SqliteDriver } from "../../store/driver";
import { uuidv7 } from "../../store/encode";
import { getLease } from "../../store/tables";
import type { Mint } from "../../team/batch";
import { claimMail } from "../../team/claim";
import { TEAM_CONSTANTS } from "../../team/constants";
import { consumable, mayResume } from "../../team/consume";
import { nextDeadline } from "../../team/deadline";
import type { DynamicChoice } from "../../team/dynamic";
import {
  materialize,
  type Rebind,
  recordedChoice,
} from "../../team/materialize";
import {
  type MemberRow,
  memberRows,
  ownRows,
  pendingFor,
} from "../../team/rows";
import type { DeferTools } from "../defer";
import { memberEntry } from "../registry";
import type { Store } from "../sqlite";
import { ancestorsOf } from "./budgets";
import { closed, pinnedOrUnavailable, principalOf, teamsUnder } from "./scan";
import { endUnbound, refuseEnded } from "./units";

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
  /** The lead's resolved defer_tools, which its members inherit unless they set their own. */
  readonly deferTools?: DeferTools;
};

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
    const pending = pendingFor(db, ownRows(db, row.thread_id));
    // A parked member runs again at a run's start (what it waits on may be answered by now), for
    // mail that may resume it, and once an ask or a wait it parked on is due. Waking on a due
    // deadline or on mail to refuse waits for a free lease: its holder does that work, and a
    // launch that can't acquire would relaunch at once, never yielding.
    if (row.state === "parked") {
      const wake = recovering || this.#wakesParked(db, branch, pending);
      return wake ? () => this.#member(row, branch) : undefined;
    }
    if (row.state === "ended") return this.#refusing(db, branch, pending);
    // A turn left open with its lease free is resumed (hostless recovery).
    const stranded =
      row.state === "running" && (recovering || this.#free(db, branch));
    const woken = this.#claim(db, pending.filter(consumable));
    return woken || stranded ? () => this.#member(row, branch) : undefined;
  }

  /** An ended member's pending mail is refused, once its lease is free. */
  #refusing(
    db: SqliteDriver,
    branch: BranchId,
    pending: readonly MailEnvelope[],
  ): (() => Promise<void>) | undefined {
    return pending.length > 0 && this.#free(db, branch)
      ? async () => refuseEnded(this.#env, branch)
      : undefined;
  }

  /** Mail that may resume the parked member, or, with its lease free, a due ask or wait. */
  #wakesParked(
    db: SqliteDriver,
    branch: BranchId,
    pending: readonly MailEnvelope[],
  ): boolean {
    return (
      this.#claim(db, pending.filter(mayResume)) ||
      (this.#free(db, branch) && this.#due(db, branch))
    );
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

  /**
   * An ask or a wait the parked member waits on is due: its writer closes it.
   * ponytail: reads the member's log each pass; keep its next deadline per head if parked members
   * grow many.
   */
  #due(db: SqliteDriver, branch: BranchId): boolean {
    const read = this.#env.log.read(branch);
    if (!read.ok) return false;
    const next = nextDeadline({ db, chain: read.value, branchId: branch });
    return next !== undefined && next <= this.#env.log.now();
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
      rebind: (agent, configHash, choice) =>
        this.#rebind(agent, configHash, choice),
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

  /**
   * Rebinds a member's definition by name in this process (design §4.10, prework); a dynamic
   * member's with its recorded choice, never re-resolved.
   */
  async #rebind(
    agent: string,
    configHash: string,
    choice: DynamicChoice | undefined,
  ): Promise<Rebind> {
    const handle = this.#env.agents.get(agent);
    const entry = handle === undefined ? undefined : memberEntry(handle);
    if (entry === undefined) return { status: "pin_unavailable" };
    const pinned = await pinnedOrUnavailable(() =>
      entry.pinned(this.#env.deferTools, choice),
    );
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
    const choice = recordedChoice(this.#env.log, row, task.data.mail_id);
    const rebind = await this.#rebind(
      row.agent,
      started?.data.config_hash ?? "",
      choice,
    );
    if (entry === undefined || rebind.status !== "ok")
      return endUnbound(
        this.#env,
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
      ...(choice === undefined ? {} : { dynamic: choice }),
      ...(this.#env.deferTools === undefined
        ? {}
        : { deferTools: this.#env.deferTools }),
    });
  }
}

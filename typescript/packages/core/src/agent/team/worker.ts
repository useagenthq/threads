import { BranchId, type MailEnvelope, type TeamId } from "../../log";
import { knownEvents } from "../../reduce";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { reading, type StoreDriver } from "../../store/driver";
import type { Mint } from "../../team/batch";
import { claimMail } from "../../team/claim";
import { TEAM_CONSTANTS } from "../../team/constants";
import { mayResume } from "../../team/consume";
import type { DynamicChoice } from "../../team/dynamic";
import { type Rebind, recordedChoice } from "../../team/materialize";
import {
  cancelPendingFor,
  type MemberRow,
  memberRows,
  ownRows,
  pendingFor,
} from "../../team/rows";
import type { DeferTools } from "../defer";
import { memberEntry } from "../registry";
import type { Store } from "../sqlite";
import { ancestorsOf, startedCap } from "./budgets";
import { takeTeamLogMail } from "./log-mail";
import {
  closed,
  deadlineDue,
  leaseFree,
  principalOf,
  teamsUnder,
} from "./scan";
import { materializeOrLater, rebindMember } from "./setup";
import { endUnbound, refuseEnded } from "./units";

// The in-process team worker (spec/schema/README.md, "Teams"; decision 17): while a lead's run
// is open, it materializes every starting member of the lead's team (and of each nested team),
// runs each member whose mail is pending, refuses mail that reaches an ended member, and resumes
// a member whose turn was left open with its lease free (hostless recovery). The lead consumes
// its own mail in its run. One member branch runs at a time; members run concurrently.
//
// A cancel for a member in flight aborts its model call at once (design §4.14): the worker owns
// that run's abort signal, and the member applies the cancel at its next step boundary. A member
// whose setup failed for now (an MCP connect, an adapter's setup) is left as it is and tried again
// with backoff, never ended.

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
  /** How many setup failures in a row end a member setup_failed; tests inject fewer. */
  readonly setupAttempts?: number;
};

/** The first and the longest wait before a member whose unit did nothing is tried again. */
const BACKOFF = { firstMs: 250, maxMs: TEAM_CONSTANTS.claimTtlMs } as const;

type Unit = (signal: AbortSignal) => Promise<boolean>;

export class TeamWorker {
  readonly #env: WorkerEnv;
  /** Member runs in flight, by member thread, with the abort each one's signal follows. */
  readonly #running = new Map<
    string,
    { readonly done: Promise<void>; readonly abort: AbortController }
  >();
  /** How many times in a row each member's setup has failed for now. */
  readonly #setups = new Map<string, number>();
  /** Members whose last unit did nothing: not before `at`, then twice the wait. */
  readonly #backoff = new Map<string, { at: number; waitMs: number }>();
  #changed = Promise.withResolvers<void>();
  #failure: { readonly error: unknown } | undefined;
  #stopped = false;
  /** The run-start pass hasn't finished: it may still launch a member. */
  #recovering = false;
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

  /**
   * A member run is in flight, the run-start pass may still launch one, or one failed: a lead
   * parked on its members then waits on progress, which rejects with the failure.
   */
  readonly busy = (): boolean =>
    this.#recovering || this.#running.size > 0 || this.#failure !== undefined;

  start(): void {
    this.#recovering = true;
    this.#loop = this.#run();
  }

  /**
   * Stops looking for work and waits for the member runs in flight, then throws a member run's
   * bug if there was one. When the lead closed its team those runs are aborted first: each
   * applies its cancel at its next step and ends.
   */
  async stop(): Promise<void> {
    this.#stopped = true;
    this.notify();
    await this.#loop;
    if (await closed(this.#env.log.driver, this.#env.team))
      for (const run of this.#running.values()) run.abort.abort();
    await Promise.allSettled([...this.#running.values()].map((r) => r.done));
    // A member run's bug is never lost, whatever the lead's run was waiting on when it ended.
    if (this.#failure !== undefined) throw this.#failure.error;
  }

  async #run(): Promise<void> {
    let recovering = true;
    while (!this.#stopped) {
      const changed = this.#changed.promise;
      try {
        await this.#pass(recovering);
      } finally {
        if (recovering) {
          this.#recovering = false;
          this.notify();
        }
      }
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

  async #pass(recovering: boolean): Promise<void> {
    const db = this.#env.log.driver;
    const now = this.#env.log.now();
    for (const team of await teamsUnder(db, this.#env.team)) {
      await takeTeamLogMail(
        this.#env.log,
        this.#env.artifacts,
        team,
        this.#env.mint,
      );
      for (const row of await reading(db, (tx) => memberRows(tx, team)))
        if (row.role === "member") await this.#visit(db, row, recovering, now);
    }
  }

  async #visit(
    db: StoreDriver,
    row: MemberRow,
    recovering: boolean,
    now: number,
  ): Promise<void> {
    const cancelled = await reading(db, (tx) =>
      cancelPendingFor(tx, row.thread_id),
    );
    const running = this.#running.get(row.thread_id);
    if (running !== undefined) {
      // A cancel reached a member in flight: stop its model call now.
      if (cancelled) running.abort.abort();
      return;
    }
    // A pending cancel is applied at once, backoff or not: it never needs the member's setup.
    const waiting = (this.#backoff.get(row.thread_id)?.at ?? 0) > now;
    if (waiting && !cancelled) return;
    const work = await this.#work(db, row, recovering, cancelled);
    if (work !== undefined) this.#launch(row.thread_id, work);
  }

  /** What a member needs now, if anything. */
  async #work(
    db: StoreDriver,
    row: MemberRow,
    recovering: boolean,
    cancelled: boolean,
  ): Promise<Unit | undefined> {
    // A closed team's members start or resume only to apply the lead's cancel.
    if (row.state !== "ended" && (await closed(db, row.team_id)) && !cancelled)
      return undefined;
    if (row.state === "starting")
      return (signal) => this.#materialize(row, signal);
    const branch = row.branch_id;
    if (branch === null) return undefined;
    const pending = await reading(db, async (tx) =>
      pendingFor(tx, await ownRows(tx, row.thread_id)),
    );
    const run: Unit = (signal) => this.#member(row, branch, signal);
    // A parked member runs again at a run's start (what it waits on may be answered by now), for
    // mail that may resume it, and once an ask or a wait it parked on is due. Waking on a due
    // deadline or on mail to refuse waits for a free lease: its holder does that work, and a
    // launch that can't acquire would relaunch at once, never yielding.
    if (row.state === "parked") {
      const wake = recovering || (await this.#wakesParked(db, branch, pending));
      return wake ? run : undefined;
    }
    if (row.state === "ended") return this.#refusing(branch, pending);
    // A turn left open with its lease free is resumed (hostless recovery).
    const stranded =
      row.state === "running" &&
      (recovering || (await leaseFree(this.#env.log, branch)));
    return (await this.#claim(db, pending)) || stranded ? run : undefined;
  }

  /** An ended member's pending mail is refused, once its lease is free. */
  async #refusing(
    branch: BranchId,
    pending: readonly MailEnvelope[],
  ): Promise<Unit | undefined> {
    return pending.length > 0 && (await leaseFree(this.#env.log, branch))
      ? () => refuseEnded(this.#env, branch)
      : undefined;
  }

  /** Mail that may resume the parked member, or, with its lease free, a due ask or wait. */
  async #wakesParked(
    db: StoreDriver,
    branch: BranchId,
    pending: readonly MailEnvelope[],
  ): Promise<boolean> {
    return (
      (await this.#claim(db, pending.filter(mayResume))) ||
      ((await leaseFree(this.#env.log, branch)) &&
        (await deadlineDue(this.#env.log, branch)))
    );
  }

  /**
   * mail.claim (design §4.6) on the first row this worker would wake the member for: a live claim
   * of another worker means that worker wakes it. Correctness never depends on it: the lease
   * holder consumes.
   */
  async #claim(
    db: StoreDriver,
    mail: readonly MailEnvelope[],
  ): Promise<boolean> {
    const first = mail[0];
    if (first === undefined) return false;
    const now = this.#env.log.now();
    const ttl = this.#env.claimTtlMs ?? TEAM_CONSTANTS.claimTtlMs;
    const claim = await claimMail(db, first.mail_id, this.#token, now, ttl);
    return claim === "claimed";
  }

  /**
   * Runs one unit for a member. One that did nothing (its lease held elsewhere, its setup failed
   * for now) backs off, so a pass never relaunches it at once; one that did something wakes the
   * next pass.
   */
  #launch(thread: string, unit: Unit): void {
    const abort = new AbortController();
    const done = (async () => {
      try {
        if (await unit(abort.signal)) {
          this.#backoff.delete(thread);
          this.notify();
        } else this.#later(thread);
      } catch (error) {
        this.#failure ??= { error };
        this.notify();
      } finally {
        this.#running.delete(thread);
      }
    })();
    this.#running.set(thread, { done, abort });
  }

  #later(thread: string): void {
    const last = this.#backoff.get(thread)?.waitMs ?? 0;
    const waitMs = Math.min(Math.max(last * 2, BACKOFF.firstMs), BACKOFF.maxMs);
    this.#backoff.set(thread, { at: this.#env.log.now() + waitMs, waitMs });
  }

  async #materialize(row: MemberRow, signal: AbortSignal): Promise<boolean> {
    const holder = `team-${crypto.randomUUID()}`;
    const got = await materializeOrLater(this.#env, row, holder, (...pin) =>
      this.#rebind(row.thread_id, ...pin),
    );
    if (got === "later") return false;
    if (!got.ok)
      throw new Error(`materialize ${row.name}: ${got.error.message}`);
    this.notify();
    if (got.value.status !== "materialized") return true;
    const branch = got.value.writer.lease.branchId;
    return this.#member(row, branch, signal, holder);
  }

  /** The member's definition rebound here (setup.ts), its setup failures counted. */
  #rebind(
    thread: string,
    agent: string,
    configHash: string,
    choice: DynamicChoice | undefined,
  ): Promise<Rebind | "later"> {
    return rebindMember(this.#env, this.#setups, thread, {
      agent,
      configHash,
      choice,
    });
  }

  /** Runs a member branch until it is idle, parked or ended. */
  async #member(
    row: MemberRow,
    branch: string,
    signal: AbortSignal,
    holder = `team-${crypto.randomUUID()}`,
  ): Promise<boolean> {
    const read = await this.#env.log.read(BranchId.parse(branch));
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
    const choice = await recordedChoice(this.#env.log, row, task.data.mail_id);
    const rebind = await this.#rebind(
      row.thread_id,
      row.agent,
      started?.data.config_hash ?? "",
      choice,
    );
    if (rebind === "later") return false;
    if (entry === undefined || rebind.status !== "ok")
      return endUnbound(
        this.#env,
        branch,
        holder,
        rebind.status === "ok" ? "pin_unavailable" : rebind.status,
      );
    const user = this.#env.signal;
    await entry.run({
      store: this.#env.store,
      thread: {
        id: row.thread_id,
        branch: BranchId.parse(branch),
        store: this.#env.store,
      },
      parent: { ...parent, relation: "team_member" },
      principal:
        (await principalOf(this.#env.log.driver, read.value, row)) ??
        task.actor.principal,
      holder,
      notify: this.notify,
      covering: [
        ...(await startedCap(this.#env.log, parent)),
        ...(await ancestorsOf(this.#env.log, parent)),
      ],
      signal: user === undefined ? signal : AbortSignal.any([user, signal]),
      ...(choice === undefined ? {} : { dynamic: choice }),
      ...(this.#env.deferTools === undefined
        ? {}
        : { deferTools: this.#env.deferTools }),
    });
    return true;
  }
}

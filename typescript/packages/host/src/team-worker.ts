import {
  BranchId,
  claimMail,
  type HostedTeam,
  type HostTeam,
  hostedTeams,
  hostTeams,
  leaseFree,
  type MemberEntry,
  pendingCallers,
  pendingHere,
  reading,
  type Supervision,
  storeConnection,
  TEAM_CONSTANTS,
  type TeamLead,
  type TeamWorker,
  takeMail,
  teamLeadOf,
  teamWorkerFor,
} from "threadsai/host";
import type { HostContext } from "./context";
import { hostPinOf, openHostTeams } from "./host-members";
import { type HostMember, restartPolicy } from "./members";
import type { Watch } from "./watch";

// The host as a team worker (design §7 Phase 2, A): each tick drives lane 21's worker for every
// open team of the store, so members materialize, take their mail, close their deadlines, apply
// their cancels and resume a turn a crash left open, with no lead's run in this process. One
// worker per team per host, and one consumer per branch, as kick already does; across processes
// the lease decides and claims only deduplicate wakes.
//
// A lead is a member too (§2.4), but no worker runs its branch: mail to an idle hosted lead is
// consumed here, which opens its turn, and the host's recovery then runs that turn on. It
// continues the request the mail belongs to and starts no new run (§2.7.2).

export class Teams {
  readonly #ctx: HostContext;
  /** host({members}): the configured host members, whose team this tick also opens and drives. */
  readonly #members: readonly HostMember[];
  /** One worker per team this host drives, until its lead runs here, or the host stops. */
  readonly #workers = new Map<HostedTeam["team_id"], TeamWorker>();
  /** Each team's lead as this process defines it: pinning it again would be the costly part. */
  readonly #leads = new Map<HostedTeam["team_id"], TeamLead>();
  /** This host's claim token: a row it claimed is one it will wake the lead for. */
  readonly #token = `host-${crypto.randomUUID()}`;
  #stopped = false;

  constructor(ctx: HostContext, members: readonly HostMember[] = []) {
    this.#ctx = ctx;
    this.#members = members;
  }

  /** One tick: a worker for every open team, and the mail an idle lead or caller has waiting. */
  async pass(watch: Watch): Promise<void> {
    if (this.#stopped) return;
    // Before the teams are listed: a tenant whose host team is not open yet gets it now.
    await openHostTeams(this.#ctx, this.#members);
    const { db } = await storeConnection(this.#ctx.store);
    const teams = await hostedTeams(db);
    const hosts = await hostTeams(db);
    const open = new Set([...teams, ...hosts].map((t) => t.team_id));
    for (const team of [...this.#workers.keys(), ...this.#leads.keys()])
      if (!open.has(team)) {
        this.#leads.delete(team);
        await this.#drop(team);
      }
    for (const team of teams) {
      await this.#ensure(team);
      await this.#wake(team, watch);
    }
    for (const team of hosts) {
      await this.#ensureHost(team);
      await this.#wakeCallers(db, team, watch);
    }
  }

  /**
   * A host team's worker: leadless, so its members are rebound against the host's own registry —
   * the policy-aware definitions `HostRunner.withPolicy` builds — never through a lead rebind. It
   * inherits no defer_tools: there is no lead to inherit them from.
   */
  async #ensureHost(team: HostTeam): Promise<void> {
    if (this.#members.length === 0 || this.#workers.has(team.team_id)) return;
    const { log, artifacts } = await this.#ctx.open(team.tenant_id);
    const worker = teamWorkerFor(
      {
        store: this.#ctx.storeFor(team.tenant_id),
        log,
        artifacts,
        team: team.team_id,
        signal: this.#ctx.stopping,
        supervision: this.#supervision(),
        hostPin: hostPinOf(this.#ctx, this.#members, team.tenant_id),
      },
      { agents: this.#entries(), deferTools: undefined },
    );
    this.#workers.set(team.team_id, worker);
    worker.start();
  }

  /** Each configured host member's restart policy, by name: the supervisor step's input. */
  #supervision(): Supervision {
    return new Map(this.#members.map((m) => [m.agent, restartPolicy(m)]));
  }

  /** Each configured host member's policy-aware member entry, by agent name. */
  #entries(): ReadonlyMap<string, MemberEntry> {
    return new Map(
      [...this.#ctx.agents.values()].flatMap((a) =>
        this.#members.some((m) => m.agent === a.name)
          ? [[a.name, a.runner.member] as const]
          : [],
      ),
    );
  }

  /**
   * A caller with pending mail and a free lease is watched, so this tick's recovery runs it on and
   * its consume takes the reply or bounce. The caller's own log is the only record of its ask.
   */
  async #wakeCallers(
    db: Parameters<typeof pendingCallers>[0],
    team: HostTeam,
    watch: Watch,
  ): Promise<void> {
    for (const row of await pendingCallers(db, team.team_id)) {
      if (this.#ctx.busy(row.to_branch_id)) continue;
      const opened = await this.#ctx.open(team.tenant_id);
      if (!(await leaseFree(opened.log, row.to_branch_id))) continue;
      // The same wake as a lead's: the mail consumed under the caller's own writer, which closes
      // its ask and resumes its park, then its run watched so this tick runs that turn on.
      if (await this.#take(opened, row.thread_id, row.to_branch_id))
        watch.add(team.tenant_id, row.thread_id, row.to_branch_id);
    }
  }

  /**
   * Stops claiming and waits for the work in flight, leaving every row durable for the next
   * host. A member run's bug is reported, never thrown: stopping a host is not its caller's
   * error.
   */
  async stop(): Promise<void> {
    this.#stopped = true;
    for (const team of [...this.#workers.keys()]) await this.#drop(team);
  }

  async #drop(team: HostedTeam["team_id"]): Promise<void> {
    const worker = this.#workers.get(team);
    this.#workers.delete(team);
    try {
      await worker?.stop();
    } catch (error) {
      console.error(`threads host: the team worker of ${team} failed`, error);
    }
  }

  /**
   * A worker for the team while no run of this process is on its lead's branch: that run drives
   * the team itself, and one consumer per branch per process is the rule (§4.6). The lead it
   * rebinds is kept, so the worker a later tick starts again costs no new pin.
   */
  async #ensure(team: HostedTeam): Promise<void> {
    const branch = team.lead_branch_id;
    if (branch === null) return;
    if (this.#ctx.busy(branch)) {
      await this.#drop(team.team_id);
      return;
    }
    if (this.#workers.has(team.team_id)) return;
    const { log, artifacts } = await this.#ctx.open(team.tenant_id);
    const known =
      this.#leads.get(team.team_id) ?? (await teamLeadOf(log, team.team_id));
    // Another host defines that lead: it drives the team, and this one leaves the rows alone.
    if (known === undefined) return;
    this.#leads.set(team.team_id, known);
    const worker = teamWorkerFor(
      {
        store: this.#ctx.storeFor(team.tenant_id),
        log,
        artifacts,
        team: team.team_id,
        signal: this.#ctx.stopping,
      },
      known,
    );
    this.#workers.set(team.team_id, worker);
    worker.start();
  }

  /**
   * A lead with a free lease: its pending mail consumed under its own writer, then the turn that
   * consume opened, or one a crash left open, is watched, so this tick's recovery runs it on. A
   * lease held elsewhere (a run of this host included) leaves both to its holder, which consumes
   * at its next step boundary.
   */
  async #wake(team: HostedTeam, watch: Watch): Promise<void> {
    const branch = team.lead_branch_id;
    const thread = team.lead_thread_id;
    if (branch === null || thread === null || this.#ctx.busy(branch)) return;
    const opened = await this.#ctx.open(team.tenant_id);
    const { log } = opened;
    if (!(await leaseFree(log, branch))) return;
    const taken = await this.#take(opened, thread, branch);
    const read = await log.read(BranchId.parse(branch));
    const open = read.ok && read.value.fold.turnOpen;
    if (taken || open) watch.add(team.tenant_id, thread, branch);
  }

  /** The lead's pending mail, claimed first so two hosts don't both wake it. */
  async #take(
    { log, artifacts }: Awaited<ReturnType<HostContext["open"]>>,
    thread: HostedTeam["lead_thread_id"] & string,
    branch: BranchId,
  ): Promise<boolean> {
    const pending = await reading(log.driver, (tx) =>
      pendingHere(tx, thread, branch),
    );
    const first = pending[0];
    if (first === undefined) return false;
    const claim = await claimMail(
      log.driver,
      first.mail_id,
      this.#token,
      log.now(),
      TEAM_CONSTANTS.claimTtlMs,
    );
    if (claim !== "claimed") return false;
    const taken = await takeMail({ log, artifacts }, branch);
    return taken !== undefined && taken.status !== "nothing_pending";
  }
}

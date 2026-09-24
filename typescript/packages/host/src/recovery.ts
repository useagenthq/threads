import {
  type BranchId,
  knownEvents,
  type Principal,
  StoreError,
  type ThreadId,
} from "@threads/core/host";
import type { HostContext, HostedAgent } from "./context";

// Runs a crash left open, run on from the log by a host's recovery pass (spec/schema/README.md,
// "API run recovery"). A watched thread is looked at on every tick until it settles. A lost lease
// is tried again on the next tick; a store outage (StoreError, wherever it met the store: reading
// the thread, or the run) after a wait that doubles from 1 s to 60 s while it lasts, said once per
// streak. Any other failure is said with its reason and not retried: the turn stays open in the
// log for a control, a new input or the next start.

type Thread = { readonly id: ThreadId; readonly branch: BranchId };

/** A store error's first wait before the next look, doubling while it lasts up to the max. */
const STORE_RETRY_MS = 1_000;
const STORE_BACKOFF_MAX_MS = 60_000;

type Streak = { readonly waitMs: number; readonly atMs: number };

export class Recovery {
  readonly #ctx: HostContext;
  /** Branches a recovery run failed on another way than a lost lease or an outage. */
  readonly #notRetried = new Set<BranchId>();
  /** Store outages in a row, by branch (or thread, before its main branch is known). */
  readonly #streaks = new Map<string, Streak>();

  constructor(ctx: HostContext) {
    this.#ctx = ctx;
  }

  /**
   * One look at a watched thread (at `branch`, else its main branch) no job of this process is
   * on: a turn left open with no run runs on from the log, then the thread's missing replies are
   * issued. "busy" when it must be looked at again on a later tick. It never waits on a run.
   */
  async look(
    tenant: string,
    id: ThreadId,
    branch?: BranchId,
  ): Promise<"done" | "busy"> {
    const key = branch ?? id;
    const streak = this.#streaks.get(key);
    if (streak !== undefined && Date.now() < streak.atMs) return "busy";
    let verdict: "done" | "busy";
    try {
      verdict = await this.#look(tenant, id, branch, key);
    } catch (error) {
      if (!(error instanceof StoreError)) throw error;
      this.failed(key, error);
      return "busy";
    }
    // A thread the watch drops leaves no streak behind.
    if (verdict === "done") this.#streaks.delete(key);
    return verdict;
  }

  /** One more store outage in the streak of `key`: the next look waits twice as long. */
  failed(key: string, error: StoreError): void {
    const streak = this.#streaks.get(key);
    if (streak === undefined)
      console.error(
        `threads host: run on ${key} hit a store error; looking again with backoff`,
        error,
      );
    const waitMs =
      streak === undefined
        ? STORE_RETRY_MS
        : Math.min(streak.waitMs * 2, STORE_BACKOFF_MAX_MS);
    this.#streaks.set(key, { waitMs, atMs: Date.now() + waitMs });
  }

  async #look(
    tenant: string,
    id: ThreadId,
    known: BranchId | undefined,
    key: string,
  ): Promise<"done" | "busy"> {
    const { log } = await this.#ctx.open(tenant);
    const main = known === undefined ? log.mainBranch(id) : undefined;
    const branch = known ?? (main?.ok === true ? main.value : undefined);
    if (branch === undefined) return "done";
    if (this.#ctx.busy(branch)) return "busy";
    // Said once when it failed; the watch drops it now.
    if (this.#notRetried.delete(branch)) return "done";
    const read = log.read(branch);
    if (!read.ok) {
      console.error(
        `threads host: run on ${branch} not recovered (${read.error.code}: ${read.error.message})`,
      );
      return "done";
    }
    const thread = { id, branch };
    const { fold } = read.value;
    if (!fold.turnOpen || fold.parked.length > 0)
      return this.#ctx.replies(tenant, thread);
    const events = knownEvents(read.value);
    const hosted = this.#ctx.agentOf(events);
    const who = events.findLast((e) => e.type === "user_input")?.actor
      .principal;
    if (hosted === undefined || who === undefined) return "done";
    // The run issues its replies as it ends; a later tick confirms the turn closed.
    void this.#rerun(hosted, tenant, who, thread, key);
    return "busy";
  }

  /** A recovery run, judged by how it ended. */
  async #rerun(
    hosted: HostedAgent,
    tenant: string,
    who: Principal,
    thread: Thread,
    key: string,
  ): Promise<void> {
    const ran = await this.#ctx.attempt(hosted, tenant, who, thread);
    if (ran.kind === "stopped") return;
    if (ran.kind === "threw" && ran.error instanceof StoreError) {
      this.failed(key, ran.error);
      return;
    }
    this.#streaks.delete(key);
    const why =
      ran.kind === "threw"
        ? ran.error instanceof Error
          ? ran.error.message
          : String(ran.error)
        : ran.result.status === "failed" &&
            ran.result.error.code !== "branch_busy"
          ? `${ran.result.error.code}: ${ran.result.error.message}`
          : undefined;
    if (why === undefined) return;
    this.#notRetried.add(thread.branch);
    console.error(`threads host: run on ${thread.branch} not retried (${why})`);
  }
}

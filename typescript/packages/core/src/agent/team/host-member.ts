import { BranchId } from "../../log";
import type { ArtifactStore } from "../../store/artifacts";
import type { Rebind } from "../../team/materialize";
import type { MemberRow } from "../../team/rows";
import type { VerifiedLog } from "../../verify";
import type { MemberEntry } from "../registry";
import { principalOf } from "./scan";
import { endUnbound } from "./units";
import type { WorkerEnv } from "./worker";

// Running a host member (spec/schema/README.md, "Teams Phase 2"): the team worker's path for a
// member with no task and no parent.
//
// A host member is a root thread of its tenant, rebound by its agent name from this host's own
// registry, under no ancestor's budget: only its own thread budget, which is a lifetime cap, and
// the run budget of whichever caller's mail opened the turn.

/**
 * A host member's pin as this process holds it, with its canonical config and its deferred
 * tools' specs stored: the config_hash a `member_started` of it may name. The lazy open, the
 * supervisor's restart and an operator's all take it from here, so one definition change moves
 * all three together.
 */
export async function pinHostMember(
  artifacts: ArtifactStore,
  entry: MemberEntry,
): Promise<string> {
  const pin = await entry.pinned(undefined);
  await artifacts.put(new TextEncoder().encode(pin.config));
  for (const bytes of pin.artifacts) await artifacts.put(bytes);
  return pin.configHash;
}

/** The branch this pass runs, as the worker read it, and what it shares with the worker. */
export type HostMemberPass = {
  readonly branch: string;
  /** Its own thread_started's config_hash: what the rebind must still produce. */
  readonly configHash: string;
  readonly chain: VerifiedLog;
  readonly holder: string;
  readonly signal: AbortSignal;
  readonly notify: () => void;
  /** The worker's rebind, with its setup failures counted. */
  readonly rebind: (
    agent: string,
    configHash: string,
  ) => Promise<Rebind | "later">;
};

/**
 * Runs the host member until it is idle, parked or ended. False: nothing ran — its setup failed
 * for now, or no mail is pending under any principal.
 */
export async function runHostMember(
  env: WorkerEnv,
  row: MemberRow,
  pass: HostMemberPass,
): Promise<boolean> {
  const rebound = await pass.rebind(row.agent, pass.configHash);
  if (rebound === "later") return false;
  const entry = env.agents.get(row.agent);
  if (entry === undefined || rebound.status !== "ok")
    return await endUnbound(
      env,
      pass.branch,
      pass.holder,
      rebound.status === "ok" ? "pin_unavailable" : rebound.status,
    );
  // One run, one authority (design §2.6): the principal of the mail this run would take. None
  // pending under any principal means there is nothing here for this run to do.
  const principal = await principalOf(env.log.driver, pass.chain, row);
  if (principal === undefined) return false;
  const user = env.signal;
  await entry.run({
    store: env.store,
    thread: {
      id: row.thread_id,
      branch: BranchId.parse(pass.branch),
      store: env.store,
    },
    principal,
    holder: pass.holder,
    notify: pass.notify,
    // A host member reserves against no ancestor thread budget: its own is a lifetime cap, and
    // the caller pays through its run budget.
    covering: [],
    signal:
      user === undefined ? pass.signal : AbortSignal.any([user, pass.signal]),
  });
  return true;
}

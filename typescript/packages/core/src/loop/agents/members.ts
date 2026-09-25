import { type Fold, loopParked } from "../../fold/state";
import { ok } from "../../result";
import type { DecideTx } from "../../store/writer";
import { isRefusal } from "../../store/writer";
import { Batch } from "../../team/batch";
import { readerOf } from "../../team/close";
import { TEAM_CONSTANTS } from "../../team/constants";
import { type ConsumeContext, consume } from "../../team/consume";
import { deadline, dueIds } from "../../team/deadline";
import { turnProvenance } from "../../team/provenance";
import { settle } from "../../team/settle";
import type { LoopEnd } from "../run";
import type { Session } from "../session";
import { BARRED, type Halt, type TeamRuntime } from "../types";
import { runStatus } from "./stop";
import { teamOf } from "./team-calls";

// A team thread between its turns (spec/schema/README.md, "Teams"): while idle, or parked only on
// what team mail resolves (a member, an ask, a wait), its pending mail is consumed, its due asks
// and waits are closed, and each turn a receipt opens (or an answer resumes) is run.

/** Parks that team mail or a deadline resolves: on a member, an ask or a wait. */
const TEAM_PARKS: ReadonlySet<string> = new Set(["member", "ask", "wait"]);

/** Whether every park of this thread is one team mail or a deadline resolves. */
export function onTeam(fold: Fold): boolean {
  return loopParked(fold).every((p) => TEAM_PARKS.has(p.kind));
}

/**
 * A team thread's turns after its input's: while idle, or parked only on team parks, its pending
 * mail is consumed and its due deadlines closed, and each turn that opens or resumes is run. A
 * member run by the team worker stops once nothing opens a turn. The lead of an in-process run
 * waits for its members until its run ends (spec/schema/README.md, "Run completion"), woken by
 * their progress, its own log moving, or the in-process poll; parked on members, it waits only
 * while the worker is running one; parked on an ask or a wait, until it is answered or due.
 */
export async function teamTurns(
  s: Session,
  first: LoopEnd,
  turn: () => Promise<LoopEnd>,
): Promise<LoopEnd> {
  const team = teamOf(s);
  let end = first;
  while (end.kind === "idle" || (end.kind === "parked" && onTeam(s.fold))) {
    // Taken before the checks, so progress made after them still wakes the wait. A worker bug
    // rejects it: the wait below rethrows it, and a progress never waited on is dropped quietly.
    const progress = team.progress?.();
    progress?.catch(() => undefined);
    const moved = s.moved();
    const halted = (await teamStep(s)) ?? (await endCancelled(s));
    if (halted !== undefined) return { kind: "halted", halt: halted };
    if (s.fold.turnOpen && loopParked(s.fold).length === 0) end = await turn();
    else if (progress === undefined || !waits(s, team)) return idleOrParked(s);
    else await waitFor(progress, moved);
  }
  return end;
}

/** The lead waits: on an ask or a wait (a deadline bounds it), on members the worker is running,
 * or, idle, while its run is open. */
function waits(s: Session, team: TeamRuntime): boolean {
  const parked = loopParked(s.fold);
  if (parked.some((p) => p.kind === "ask" || p.kind === "wait")) return true;
  if (parked.length > 0) return team.busy?.() === true;
  return runStatus(s) === "running";
}

function idleOrParked(s: Session): LoopEnd {
  return loopParked(s.fold).length > 0 ? { kind: "parked" } : { kind: "idle" };
}

async function waitFor(
  progress: Promise<void>,
  moved: Promise<void>,
): Promise<void> {
  const poll = Promise.withResolvers<void>();
  const timer = setTimeout(poll.resolve, TEAM_CONSTANTS.wakePollInProcessMs);
  await Promise.race([progress, moved, poll.promise]);
  clearTimeout(timer);
}

/** One step between turns: the pending mail, then every ask or wait whose deadline has passed. */
async function teamStep(s: Session): Promise<Halt | undefined> {
  return (
    (await consumeMail(s)) ??
    decided(s, async (ctx) => {
      for (const id of await dueIds(ctx, ctx.batch.now))
        await deadline(ctx, id);
    })
  );
}

/**
 * A member whose tree cancel was applied while no turn was open ends cancelled (the barrier
 * rules): member_ended{cancelled}, with its notifications and refusals, in one append. An open
 * turn ends through the loop's cancellation step instead.
 */
async function endCancelled(s: Session): Promise<Halt | undefined> {
  const { team } = s.fold;
  if (!team.stopped || team.ended || s.fold.turnOpen) return undefined;
  return decided(s, async (ctx) => {
    const provenance = await turnProvenance(ctx.tx, ctx.chain);
    if (provenance === undefined)
      throw new Error("a member's task opened a turn");
    const put = (text: string) => s.store(text, "text/plain");
    await settle({ ...ctx, provenance, put }, { status: "cancelled" });
  });
}

/**
 * At a step boundary, a live holder applies a cancel that reached it (design §4.14): the consume
 * takes it as control mail and leaves the barrier, so nothing new starts. Undefined: none pending.
 */
export async function takeCancels(s: Session): Promise<Halt | undefined> {
  const pending = await s.config.team?.cancelPending?.();
  return pending === true ? consumeMail(s) : undefined;
}

/** mail.consume under this writer: a receipt that opens a turn leaves the loop a turn to run. */
function consumeMail(s: Session): Promise<Halt | undefined> {
  return decided(s, async (ctx) => {
    await consume(ctx);
  });
}

async function decided(
  s: Session,
  step: (ctx: ConsumeContext) => Promise<void>,
): Promise<Halt | undefined> {
  const team = teamOf(s);
  const appended = await s.appendDecided(async (tx: DecideTx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, team.mint);
    await step({
      tx: tx.tx,
      chain: tx.chain,
      batch,
      threadId: s.threadId,
      branchId: s.branchId,
      read: readerOf(s.artifacts),
      ...(team.principal === undefined ? {} : { principal: team.principal }),
    });
    return ok(batch.drafts);
  });
  if (appended === BARRED || isRefusal(appended)) return undefined;
  return appended;
}

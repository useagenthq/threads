import { type Fold, loopParked } from "../../fold/state";
import { ok } from "../../result";
import type { DecideTx } from "../../store/writer";
import { isRefusal } from "../../store/writer";
import { Batch } from "../../team/batch";
import { readerOf } from "../../team/close";
import { TEAM_CONSTANTS } from "../../team/constants";
import { type ConsumeContext, consume } from "../../team/consume";
import { deadline, dueIds } from "../../team/deadline";
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
    // Taken before the checks, so progress made after them still wakes the wait.
    const progress = team.progress?.();
    const moved = s.moved();
    const halted = teamStep(s);
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
function teamStep(s: Session): Halt | undefined {
  return (
    consumeMail(s) ??
    decided(s, (ctx) => {
      for (const id of dueIds(ctx, ctx.batch.now)) deadline(ctx, id);
    })
  );
}

/** mail.consume under this writer: a receipt that opens a turn leaves the loop a turn to run. */
function consumeMail(s: Session): Halt | undefined {
  return decided(s, (ctx) => {
    consume(ctx);
  });
}

function decided(
  s: Session,
  step: (ctx: ConsumeContext) => void,
): Halt | undefined {
  const team = teamOf(s);
  const appended = s.appendDecided((tx: DecideTx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, team.mint);
    step({
      db: tx.db,
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

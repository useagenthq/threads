import type { EventOf } from "../../fold/state";
import { ThreadId } from "../../log";
import { ok } from "../../result";
import { uuidv7 } from "../../store/encode";
import { isRefusal } from "../../store/writer";
import { Batch } from "../../team/batch";
import type { CallContext } from "../../team/call";
import { TEAM_CONSTANTS } from "../../team/constants";
import { consume } from "../../team/consume";
import { send, start } from "../../team/ops";
import { SendInput, StartInput } from "../../tools/team-inputs";
import { roomFor } from "../ledger";
import type { LoopEnd } from "../run";
import type { Session } from "../session";
import { BARRED, type Halt, type TeamRuntime } from "../types";
import { runStatus } from "./stop";

// The team tools start and send (spec/schema/README.md, "Teams", Model tools), run by the loop
// like any framework tool: each is one decided append under the caller's writer, holding the
// policy decision, the op's events and the call's one result, so a call re-dispatched after a
// crash either finds that append or makes it now, never twice.

const utf8 = new TextEncoder();

function teamOf(s: Session): TeamRuntime {
  const team = s.config.team;
  if (team === undefined) throw new Error("a team tool outside a team");
  return team;
}

/** One team op decided under the caller's writer. */
function decided(
  s: Session,
  call: EventOf<"tool_call">,
  op: (ctx: CallContext) => void,
): Halt | undefined {
  const team = teamOf(s);
  const appended = s.appendDecided((tx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, team.mint);
    op({
      db: tx.db,
      chain: tx.chain,
      batch,
      call,
      put: (text) => s.store(text, "text/plain"),
    });
    return ok(batch.drafts);
  });
  // A cancel landed first: the cancellation step closes the call.
  if (appended === BARRED) return undefined;
  if (isRefusal(appended)) throw new Error("a team op records its refusals");
  return appended;
}

/** member.start: the listed agent's config is stored before the append that pins it. */
export async function startTool(
  s: Session,
  call: EventOf<"tool_call">,
): Promise<Halt | undefined> {
  const team = teamOf(s);
  const args = StartInput.parse(call.data.input);
  const pinned = await team.pin(args.agent);
  if (pinned !== undefined) s.artifacts.put(utf8.encode(pinned.config));
  return decided(s, call, (ctx) =>
    start(ctx, args, {
      agents: new Map(
        pinned === undefined
          ? []
          : [[args.agent, { configHash: pinned.configHash }]],
      ),
      limits: team.limits,
      headroom: () => pinned !== undefined && roomFor(s, pinned),
      threadId: ThreadId.parse(uuidv7(s.now())),
    }),
  );
}

export function sendTool(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const team = teamOf(s);
  const args = SendInput.parse(call.data.input);
  return decided(s, call, (ctx) => send(ctx, args, team.limits));
}

/**
 * A team thread's turns after its input's: while idle, or parked only on its members, its
 * pending mail is consumed and each turn a receipt opens is run. A member run by the team worker
 * stops once nothing opens a turn. The lead of an in-process run waits for its members until its
 * run ends (spec/schema/README.md, "Run completion"), woken by their progress, its own log moving,
 * or the in-process poll; parked on members, it waits only while the worker is running one.
 */
export async function teamTurns(
  s: Session,
  first: LoopEnd,
  turn: () => Promise<LoopEnd>,
): Promise<LoopEnd> {
  const team = teamOf(s);
  let end = first;
  while (end.kind === "idle" || (end.kind === "parked" && onMembers(s))) {
    // Taken before the checks, so progress made after them still wakes the wait.
    const progress = team.progress?.();
    const moved = s.moved();
    const halted = consumeMail(s);
    if (halted !== undefined) return { kind: "halted", halt: halted };
    if (s.fold.turnOpen) end = await turn();
    else if (progress === undefined || !waits(s, team)) return idleOrParked(s);
    else await waitFor(progress, moved);
  }
  return end;
}

/** Whether every park of this thread is on one of its members (a {kind: member} park). */
function onMembers(s: Session): boolean {
  return s.fold.parked.every((p) => p.kind === "member");
}

/** The lead waits: parked only on members the worker is running, or its run still open. */
function waits(s: Session, team: TeamRuntime): boolean {
  if (s.fold.parked.length > 0) return onMembers(s) && team.busy?.() === true;
  return runStatus(s) === "running";
}

function idleOrParked(s: Session): LoopEnd {
  return s.fold.parked.length > 0 ? { kind: "parked" } : { kind: "idle" };
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

/**
 * mail.consume under this writer: its pending mail, taken as the consume rules say. A receipt
 * that opens a turn leaves the loop a turn to run.
 */
export function consumeMail(s: Session): Halt | undefined {
  const team = teamOf(s);
  const appended = s.appendDecided((tx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, team.mint);
    consume({
      db: tx.db,
      chain: tx.chain,
      batch,
      threadId: s.threadId,
      branchId: s.branchId,
      ...(team.principal === undefined ? {} : { principal: team.principal }),
    });
    return ok(batch.drafts);
  });
  if (appended === BARRED || isRefusal(appended)) return undefined;
  return appended;
}

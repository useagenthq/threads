import type { EventOf } from "../../fold/state";
import { ThreadId } from "../../log";
import { ok } from "../../result";
import { uuidv7 } from "../../store/encode";
import { isRefusal } from "../../store/writer";
import { ask, reply } from "../../team/ask";
import { Batch } from "../../team/batch";
import { type CallContext, callRequest, named } from "../../team/call";
import { readerOf } from "../../team/close";
import { send, start } from "../../team/ops";
import { monitor, wait } from "../../team/watch";
import {
  AskInput,
  MonitorInput,
  ReplyInput,
  SendInput,
  StartInput,
  WaitInput,
} from "../../tools/team-inputs";
import { roomFor, roomIn } from "../ledger";
import type { Session } from "../session";
import { BARRED, type Halt, type TeamRuntime } from "../types";
import { listedOf, startPin } from "./start-pin";

// The team tools (spec/schema/README.md, "Teams", Model tools), run by the loop like any framework
// tool: each is one decided append under the caller's writer, holding the policy decision, the
// op's events and the call's one result, so a call re-dispatched after a crash either finds that
// append or makes it now, never twice. An ask or a wait stays a pending call: its re-dispatch only
// parks, and the writer's close records its one result.

export function teamOf(s: Session): TeamRuntime {
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
      read: readerOf(s.artifacts),
    });
    return ok(batch.drafts);
  });
  // A cancel landed first: the cancellation step closes the call.
  if (appended === BARRED) return undefined;
  if (isRefusal(appended)) throw new Error("a team op records its refusals");
  return appended;
}

/**
 * member.start: the start's chosen fields are resolved against the listed agent (a dynamic
 * agent's template), and the member's config is stored before the append that pins it.
 */
export async function startTool(
  s: Session,
  call: EventOf<"tool_call">,
): Promise<Halt | undefined> {
  const team = teamOf(s);
  const args = StartInput.parse(call.data.input);
  const { pinned, resolved } = await startPin(
    team.pin,
    s.artifacts,
    args,
    starterOf(s),
  );
  return decided(s, call, (ctx) =>
    start(callRequest(ctx), args, {
      agents: listedOf(args.agent, pinned),
      resolved,
      limits: team.limits,
      headroom: () => pinned !== undefined && roomFor(s, pinned),
      threadId: ThreadId.parse(uuidv7(s.now())),
    }),
  );
}

/**
 * The name the block says wrote it: the calling lead's agent name, which is its member name in
 * the team it leads (so a rebind, which reads the task's sender, finds the same name).
 */
function starterOf(s: Session): string {
  const name = s.config.agents?.name;
  if (name === undefined) throw new Error("a team tool outside an agent");
  return name;
}

export function sendTool(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const team = teamOf(s);
  const args = SendInput.parse(call.data.input);
  return decided(s, call, (ctx) =>
    send(callRequest(ctx), named(ctx, args.to), args.text, team.limits),
  );
}

/** ask.open; headroom is on the recipient's budgets, read from its log. */
export function askTool(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const team = teamOf(s);
  const args = AskInput.parse(call.data.input);
  return decided(s, call, (ctx) => {
    ask(ctx, args, {
      limits: team.limits,
      headroom: (row) => {
        const to = team.recipient(row);
        return to === undefined || roomIn(s, to);
      },
    });
  });
}

export function replyTool(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const args = ReplyInput.parse(call.data.input);
  return decided(s, call, (ctx) => {
    reply(ctx, args);
  });
}

export function waitTool(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const args = WaitInput.parse(call.data.input);
  return decided(s, call, (ctx) => {
    wait(ctx, args);
  });
}

export function monitorTool(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const args = MonitorInput.parse(call.data.input);
  return decided(s, call, (ctx) => {
    monitor(ctx, args);
  });
}

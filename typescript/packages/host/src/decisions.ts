import type { Thread } from "@threads/core";
import {
  err,
  knownEvents,
  type Principal,
  type Result,
} from "@threads/core/host";
import type { z } from "zod";
import type { HostContext } from "./context";
import type { Failure } from "./errors";
import type { ApprovalDecision } from "./schemas";

// The human-in-the-loop controls every route shares (the REST routes and the UI routes): an
// approval decision under approval authority, an ask_user answer, and continuing the thread
// from the log once one is recorded. One implementation, whichever route a human used.

type Recorded = Result<{ readonly event_id: string }, Failure>;

/** forbidden unless the caller has approval authority on the thread's root run. */
export async function decideChallenge(
  ctx: HostContext,
  principal: Principal,
  thread: Thread,
  challengeId: string,
  body: z.infer<typeof ApprovalDecision>,
): Promise<Recorded> {
  const may = await ctx.mayApprove(principal.tenant, thread.id, principal);
  if (!may)
    return err({
      code: "forbidden",
      message: "this principal may not approve for this run",
    });
  const done =
    body.decision === "grant"
      ? await thread.approve(challengeId, principal, {
          ...(body.remember_rule === undefined
            ? {}
            : { rememberRule: body.remember_rule }),
        })
      : await thread.deny(challengeId, principal, {
          ...(body.reason === undefined ? {} : { reason: body.reason }),
        });
  if (done.ok) await resumeThread(ctx, principal, thread);
  return done;
}

/** An ask_user answer, from the principal whose input opened the asking turn. */
export async function answerQuestion(
  ctx: HostContext,
  principal: Principal,
  thread: Thread,
  callId: string,
  answer: string | readonly string[],
): Promise<Recorded> {
  const done = await thread.answer(callId, answer, principal);
  if (done.ok) await resumeThread(ctx, principal, thread);
  return done;
}

/**
 * After a control appended, the branch continues from the log (a resumed or a cancel). A
 * subagent's thread continues through its root: the parent parked on it re-runs it and resumes.
 * With `wait`, until that run has stopped (a cancel closing the turn before a new input).
 */
export async function resumeThread(
  ctx: HostContext,
  principal: Principal,
  thread: Thread,
  wait = false,
): Promise<void> {
  const { log } = await ctx.open(principal.tenant);
  let at = { id: thread.id, branch: thread.branch };
  for (;;) {
    const read = await log.read(at.branch);
    if (!read.ok) return;
    const events = knownEvents(read.value);
    const started = events.find((e) => e.type === "thread_started");
    const parent =
      started?.type === "thread_started" ? started.data.parent : undefined;
    if (parent?.relation === "subagent") {
      at = { id: parent.thread_id, branch: parent.branch_id };
      continue;
    }
    const hosted = ctx.agentOf(events);
    if (hosted === undefined) return;
    const running = ctx.resume(hosted, principal.tenant, principal, at);
    if (wait) await running;
    return;
  }
}

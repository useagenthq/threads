import { err, ok, type Principal, type Result } from "@threads/core/host";
import type { HostContext } from "../context";
import { answerQuestion, decideChallenge } from "../decisions";
import type { Failure } from "../errors";
import { Answer } from "../schemas";
import type { AiSdkPart } from "./bodies";
import { type Log, readLog, SETTLED_MEANWHILE } from "./common";
import { decisionOf } from "./settled";

// What an assistant-last AI SDK request records (spec/schema/ui/README.md, "AI SDK bodies"):
// each approval-responded tool part's decision and an ask_user part's answer to the open
// question, through the same controls as the REST routes. A decision the log already holds
// with the same value is a no-op; another value is approval_duplicate. One decided by someone
// else after this request read the log is judged the same way, against the log read again.

/** The event ids of what it newly recorded, in order. */
export async function recordParts(
  ctx: HostContext,
  principal: Principal,
  log: Log,
  parts: readonly AiSdkPart[],
): Promise<Result<readonly string[], Failure>> {
  const recorded: string[] = [];
  for (const part of parts) {
    const done = await recordPart(ctx, principal, log, part);
    if (!done.ok) return done;
    if (done.value !== undefined) recorded.push(done.value);
  }
  return ok(recorded);
}

async function recordPart(
  ctx: HostContext,
  principal: Principal,
  log: Log,
  part: AiSdkPart,
): Promise<Result<string | undefined, Failure>> {
  const tool = part.type.startsWith("tool-") || part.type === "dynamic-tool";
  if (tool && part.state === "approval-responded")
    return approval(ctx, principal, log, part);
  if (part.type === "tool-ask_user" && part.state === "output-available")
    return answer(ctx, principal, log, part);
  return ok(undefined);
}

async function approval(
  ctx: HostContext,
  principal: Principal,
  log: Log,
  part: AiSdkPart,
): Promise<Result<string | undefined, Failure>> {
  const approved = part.approval?.approved;
  if (part.approval === undefined || approved === undefined)
    return err({
      code: "invalid_request",
      message: "an approval-responded part needs approval.approved",
    });
  const challenge = part.approval.id;
  const logged = alreadyDecided(log, challenge, approved);
  if (logged !== undefined) return logged;
  const reason = part.approval.reason;
  const done = await decideChallenge(ctx, principal, log.thread, challenge, {
    decision: approved ? "grant" : "deny",
    ...(approved || reason === undefined ? {} : { reason }),
  });
  if (done.ok) return ok(done.value.event_id);
  if (!SETTLED_MEANWHILE.has(done.error.code)) return done;
  const now = await readLog(ctx, principal.tenant, log.thread);
  return (now && alreadyDecided(now, challenge, approved)) ?? done;
}

/** The answer to a challenge the log already decided: a no-op, or approval_duplicate. */
function alreadyDecided(
  log: Log,
  challenge: string,
  approved: boolean,
): Result<undefined, Failure> | undefined {
  const logged = decisionOf(log.events, challenge);
  if (logged === undefined) return undefined;
  return logged === (approved ? "granted" : "denied")
    ? ok(undefined)
    : err({
        code: "approval_duplicate",
        message: `challenge ${challenge} is already ${logged}`,
      });
}

async function answer(
  ctx: HostContext,
  principal: Principal,
  log: Log,
  part: AiSdkPart,
): Promise<Result<string | undefined, Failure>> {
  const callId = part.toolCallId ?? "";
  const open = log.parked.some((a) => a.kind === "input" && a.id === callId);
  if (!open) return ok(undefined);
  const parsed = Answer.safeParse({ answer: part.output });
  if (!parsed.success)
    return err({
      code: "invalid_request",
      message:
        "an ask_user output must be text or a list of the chosen options",
    });
  const done = await answerQuestion(
    ctx,
    principal,
    log.thread,
    callId,
    parsed.data.answer,
  );
  if (done.ok) return ok(done.value.event_id);
  // Answered or closed meanwhile: ignored, like any answer to a question no longer open.
  return done.error.code === "no_open_question" ? ok(undefined) : done;
}

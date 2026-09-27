import type { EventDraft, ToolContext, ToolRun } from "@threads/core/adapter";
import { err, ok, type Result } from "@threads/core/adapter";
import type { Remote } from "../a2a";
import {
  type A2aFault,
  type Answer,
  IDEMPOTENT_SEND,
  type PinnedCard,
  type Sending,
  type Task,
  textOf,
} from "../protocol";
import { resolveCard } from "./card";
import { claimOf, contextIdOf, messageIdOf } from "./derive";
import {
  follow,
  preview,
  sendStored,
  sent,
  statusOf,
  taskText,
} from "./exchange";
import { calledIn, inboundClaim, observed, ownsTask } from "./log";
import { requestBody } from "./request";
import { stateDrafts } from "./states";

// The outbound send, as the uncertainty contract's Part 1 exactly: one append of remote_call and
// effect_begin before any byte leaves, a commit on the peer's receipt, and nothing else. A lost
// answer is never a failure result and never a silent re-send — it is `unknown`, and the loop parks
// it (invariant 3).

export type SendArgs = {
  readonly message: string;
  readonly taskId: string | undefined;
};

/**
 * What this attempt must make durable with its `effect_begin`: the card on the thread's first call
 * of the remote, then the `remote_call` holding the exact bytes. A refusal here sends nothing.
 */
export async function beginSend(
  remote: Remote,
  threadId: string,
  args: SendArgs,
  ctx: ToolContext,
  sending: Sending,
): Promise<Result<readonly EventDraft[], string>> {
  const events = ctx.events();
  if (args.taskId !== undefined && !ownsTask(events, remote.name, args.taskId))
    return err(
      `unknown_task: ${args.taskId} was not created by this conversation`,
    );
  // An earlier attempt of this call already stored its bytes; rule 58 allows only one remote_call.
  if (calledIn(events, ctx.callId) !== undefined) return ok([]);
  const card = await resolveCard(remote, ctx, sending);
  if ("error" in card) return err(card.error);
  const claim = inboundClaim(events);
  const body = requestBody({
    wire: card.card.wire,
    messageId: messageIdOf(ctx.branchId, ctx.callId),
    contextId: contextIdOf(threadId, remote.name),
    taskId: args.taskId,
    text: args.message,
    metadata:
      remote.provenance === "none"
        ? undefined
        : claimOf(
            ctx.principal.tenant,
            claim.request ?? threadId,
            claim.hops + 1,
          ),
  });
  const requestRef = await ctx.store(body.bytes, "application/json");
  return ok([
    ...(card.draft === undefined ? [] : [card.draft]),
    {
      type: "remote_call",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        call_id: ctx.callId,
        remote: remote.name,
        operation: "send_message",
        message_id: body.messageId,
        context_id: body.contextId,
        ...(args.taskId === undefined ? {} : { task_id: args.taskId }),
        request_ref: requestRef,
      },
    },
  ]);
}

/** Sends the bytes the `remote_call` named, and reports exactly what the attempt established. */
export async function runSend(
  remote: Remote,
  ctx: ToolContext,
  sending: Sending,
): Promise<ToolRun> {
  const events = ctx.events();
  const called = calledIn(events, ctx.callId);
  if (called === undefined)
    // Unreachable: begin appends it in the same transaction as the effect_begin.
    return { kind: "unknown", reason: "transport_error" };
  const card = await resolveCard(remote, ctx, sending);
  if ("error" in card) return failure(card.error);
  const stored = await ctx.read(called.requestRef);
  if (!stored.ok) return failure(`the stored request could not be read`);
  const answer = await sendStored(
    card.card.wire,
    new TextDecoder().decode(stored.value),
    headers(card.card, sending),
  );
  return answered(answer, card.card, ctx, sending);
}

async function answered(
  answer: Answer,
  card: PinnedCard,
  ctx: ToolContext,
  sending: Sending,
): Promise<ToolRun> {
  switch (answer.kind) {
    case "not_sent":
      return { kind: "not_sent" };
    case "unknown":
      return { kind: "unknown", reason: answer.reason };
    case "stream":
      // SendMessage is not a streaming operation: an SSE answer to it is unreadable, and the
      // request was received, so the outcome is in doubt.
      return { kind: "unknown", reason: "transport_error" };
    case "fault":
      return faulted(answer.fault);
    case "ok": {
      const read = sent(answer.value);
      if (read.kind === "fault") return faulted(read.fault);
      if (read.kind === "message")
        // A bare Message is final: there is no task to follow and no receipt to hold.
        return {
          kind: "done",
          output: textOf(read.message.parts),
          isError: false,
        };
      return committed(read.task, card, ctx, sending);
    }
  }
}

async function committed(
  first: Task,
  card: PinnedCard,
  ctx: ToolContext,
  sending: Sending,
): Promise<ToolRun> {
  const followed = await follow(
    card.wire,
    first,
    headers(card, sending),
    Date.now() + sending.timeoutMs,
  );
  const status = statusOf(followed.task.status.state);
  const drafts = await stateDrafts(
    ctx,
    ctx.callId,
    followed.seen,
    observed(ctx.events(), ctx.callId),
  );
  return {
    kind: "done",
    output: preview(status, followed.task.id, taskText(followed.task)),
    isError: status === "failed" || status === "rejected",
    receipt: followed.task.id,
    events: drafts,
  };
}

/** A peer's A2A error is an answer, not a doubt: the call fails with it and the effect commits. */
function faulted(f: A2aFault): ToolRun {
  return { kind: "done", output: `${f.name}: ${f.message}`, isError: true };
}

/**
 * A refusal the run reaches after its begin is durable. It changed nothing at the partner, but the
 * attempt is recorded, so it is reported as an error result rather than as uncertainty.
 */
function failure(why: string): ToolRun {
  return { kind: "done", output: why, isError: true };
}

/** The credential and the extension header this attempt sends; neither is ever stored. */
export function headers(card: PinnedCard, sending: Sending): Sending {
  return {
    ...sending,
    ...(card.dedupWindowMs === undefined
      ? {}
      : { extensions: [IDEMPOTENT_SEND] }),
  };
}

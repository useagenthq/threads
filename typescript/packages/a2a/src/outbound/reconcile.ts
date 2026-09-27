import type { LookupResult, ToolContext } from "threadsai/adapter";
import type { Remote } from "../a2a";
import {
  call,
  ListTasksResponse,
  MAX_PAGE_SIZE,
  type Sending,
  type Task,
} from "../protocol";
import { resolveCard } from "./card";
import { preview, statusOf, taskText } from "./exchange";
import { calledIn } from "./log";
import { listParams } from "./request";
import { headers } from "./send";

// Reconciliation: the only thing that can turn a lost answer into a confirmed success. It asks the
// partner for the tasks of this call's context and looks for our own messageId in one's history.
//
// The lookup's finality is NONFINAL, and that is the whole point: a ListTasks that finds nothing
// may be looking while the request is still in flight, or at a history the peer has truncated, so
// it never proves absence and never settles the park by itself (30-uncertainty.md Part 1).

export async function reconcileSend(
  remote: Remote,
  ctx: ToolContext,
  sending: Sending,
): Promise<LookupResult<string>> {
  const called = calledIn(ctx.events(), ctx.callId);
  if (called === undefined)
    return {
      status: "unknown",
      reason: "the call has no remote_call to look up",
    };
  const card = await resolveCard(remote, ctx, sending);
  if ("error" in card) return { status: "unknown", reason: card.error };
  const answer = await call(
    card.card.wire,
    "ListTasks",
    listParams(called.contextId, MAX_PAGE_SIZE),
    headers(card.card, sending),
  );
  if (answer.kind === "fault")
    return {
      status: "unknown",
      reason: `${answer.fault.name}: ${answer.fault.message}`,
    };
  if (answer.kind !== "ok")
    return {
      status: "unknown",
      reason: `the lookup did not answer: ${answer.kind}`,
    };
  const page = ListTasksResponse.safeParse(answer.value);
  if (!page.success)
    return {
      status: "unknown",
      reason: "the peer's task list could not be read",
    };
  const mine = page.data.tasks.find((t) => carries(t, called.messageId));
  // Reported as what we saw; whether "nothing here" settles anything is the tool's declared
  // finality to decide, never this answer's, and for A2A it is nonfinal.
  return mine === undefined
    ? { status: "not_found" }
    : {
        status: "found",
        value: preview(statusOf(mine.status.state), mine.id, taskText(mine)),
      };
}

/** Our own receipt for the send: a task whose history holds the messageId this call derived. */
function carries(task: Task, messageId: string): boolean {
  return (task.history ?? []).some((m) => m.messageId === messageId);
}

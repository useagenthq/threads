import type { ToolContext, ToolRun } from "threadsai/adapter";
import type { Remote } from "../a2a";
import type { Sending } from "../protocol";
import { resolveCard } from "./card";
import { follow, getTask, preview, sent, statusOf, taskText } from "./exchange";
import { observed, ownsTask, taskOwner } from "./log";
import { headers } from "./send";
import { stateDrafts } from "./states";

// `<name>_status`: one GetTask, or a follow to the deadline. It is read_only and sends no message,
// so it never begins an effect and never asks for approval — and it still writes
// `remote_task_state` for each state change it observes, under the call that created the task,
// because an observation belongs to the receipt it was read against (semantic rule 57).

export async function runStatus(
  remote: Remote,
  taskId: string,
  ctx: ToolContext,
  sending: Sending,
): Promise<ToolRun> {
  const events = ctx.events();
  // All tenants' calls share one host credential, so a task this thread did not create is not
  // ours to read: nothing is sent and nothing is read.
  if (!ownsTask(events, remote.name, taskId))
    return {
      kind: "done",
      output: `unknown_task: ${taskId} was not created by this conversation`,
      isError: true,
    };
  const card = await resolveCard(remote, ctx, sending);
  if ("error" in card)
    return { kind: "done", output: card.error, isError: true };
  const sendingWith = headers(card.card, sending);
  const answer = await getTask(card.card.wire, taskId, sendingWith);
  if (answer.kind === "fault")
    return {
      kind: "done",
      output: `${answer.fault.name}: ${answer.fault.message}`,
      isError: true,
    };
  if (answer.kind !== "ok")
    return {
      kind: "done",
      output: `the read failed: ${answer.kind}`,
      isError: true,
    };
  const read = sent(answer.value);
  if (read.kind !== "task")
    return {
      kind: "done",
      output:
        read.kind === "fault"
          ? `${read.fault.name}: ${read.fault.message}`
          : "the peer answered a message, not a task",
      isError: true,
    };
  const followed = await follow(
    card.card.wire,
    read.task,
    sendingWith,
    Date.now() + sending.timeoutMs,
  );
  const owner = taskOwner(events, remote.name, taskId);
  const status = statusOf(followed.task.status.state);
  return {
    kind: "done",
    output: preview(status, followed.task.id, taskText(followed.task)),
    isError: status === "failed" || status === "rejected",
    ...(owner === undefined
      ? {}
      : {
          events: await stateDrafts(
            ctx,
            owner,
            followed.seen,
            observed(events, owner),
          ),
        }),
  };
}

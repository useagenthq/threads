import { assertNever } from "../assert-never";
import type { EventOf, Fold } from "./state";

// Subagents, todos, teams, and the per-branch ids of channel and schedule deliveries.

export type AgentEvent = EventOf<
  | "agent_spawned"
  | "agent_finished"
  | "todos_updated"
  | "team_task_created"
  | "team_task_claimed"
  | "team_task_updated"
  | "team_message"
  | "channel_delivery"
  | "schedule_fired"
  | "schedule_skipped"
>;

export function applyAgents(fold: Fold, e: AgentEvent): void {
  switch (e.type) {
    case "agent_spawned":
      fold.children.set(e.data.child_thread_id, "running");
      return;
    case "agent_finished":
      fold.children.set(e.data.child_thread_id, e.data.status);
      return;
    case "todos_updated":
      fold.todos = e.data.todos;
      return;
    case "team_task_created":
      fold.tasks.set(e.data.task_id, {
        status: "open",
        blockedBy: e.data.blocked_by,
      });
      return;
    case "team_task_claimed":
    case "team_task_updated":
      applyTask(fold, e);
      return;
    case "team_message":
      fold.messageIds.add(e.data.message_id);
      return;
    case "channel_delivery":
      fold.itemKeys.add(e.data.item_key);
      return;
    case "schedule_fired":
    case "schedule_skipped":
      fold.occurrenceIds.add(e.data.occurrence_id);
      return;
    default:
      assertNever(e);
  }
}

function applyTask(
  fold: Fold,
  e: EventOf<"team_task_claimed" | "team_task_updated">,
): void {
  const task = fold.tasks.get(e.data.task_id);
  if (task === undefined) return;
  const { blockedBy } = task;
  if (e.type === "team_task_claimed") {
    fold.tasks.set(e.data.task_id, {
      status: "claimed",
      owner: e.data.member,
      blockedBy,
    });
  } else if (e.data.status === "released") {
    fold.tasks.set(e.data.task_id, { status: "open", blockedBy });
  } else {
    fold.tasks.set(e.data.task_id, { ...task, status: e.data.status });
  }
}

import { assertNever } from "../assert-never";
import type { EventOf, Fold } from "../fold/state";
import { invalid, type Violation } from "./violation";

// Rules 9 (item_key, occurrence_id), 22, 23 and 24.

/** Rule 22: one agent_finished per spawned child. */
export function checkAgentFinished(
  fold: Fold,
  e: EventOf<"agent_finished">,
): Violation {
  return fold.children.get(e.data.child_thread_id) === "running"
    ? undefined
    : invalid(
        `agent_finished for ${e.data.child_thread_id}, which is not running`,
      );
}

/** A task event's tag and data: the team tools check a claim with the same rule before appending. */
type TaskChange =
  | Pick<EventOf<"team_task_claimed">, "type" | "data">
  | Pick<EventOf<"team_task_updated">, "type" | "data">;

/** Rule 23: claim an open, unclaimed, unblocked task; update a claimed one. */
export function checkTask(fold: Fold, e: TaskChange): Violation {
  const task = fold.tasks.get(e.data.task_id);
  if (e.type === "team_task_updated")
    return task?.status === "claimed"
      ? undefined
      : invalid(
          `team_task_updated for ${e.data.task_id}, which is not claimed`,
        );
  if (task?.status !== "open")
    return invalid(
      `team_task_claimed for ${e.data.task_id}, which is not open`,
    );
  const blocked = task.blockedBy.some(
    (id) => fold.tasks.get(id)?.status !== "completed",
  );
  return blocked
    ? invalid(`team_task_claimed for ${e.data.task_id} while a blocker is open`)
    : undefined;
}

/** Rules 9, 23 and 24: ids that must be unique. */
export function checkUnique(
  fold: Fold,
  e: EventOf<
    "team_message" | "todos_updated" | "channel_delivery" | "schedule_fired"
  >,
): Violation {
  switch (e.type) {
    case "team_message":
      return unique(fold.messageIds, [e.data.message_id], "message_id");
    case "todos_updated":
      return unique(
        new Set(),
        e.data.todos.map((todo) => todo.id),
        "todo id",
      );
    case "channel_delivery":
      return unique(fold.itemKeys, [e.data.item_key], "item_key");
    case "schedule_fired":
      return unique(
        fold.occurrenceIds,
        [e.data.occurrence_id],
        "occurrence_id",
      );
    default:
      return assertNever(e);
  }
}

function unique(
  seen: ReadonlySet<string>,
  ids: readonly string[],
  what: string,
): Violation {
  const local = new Set<string>();
  for (const id of ids) {
    if (seen.has(id) || local.has(id))
      return invalid(`duplicate ${what} ${id}`);
    local.add(id);
  }
  return undefined;
}

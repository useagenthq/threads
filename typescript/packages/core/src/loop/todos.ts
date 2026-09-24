import type { EventOf, Todo } from "../fold/state";
import { TodoWriteInput } from "../tools/agent-inputs";
import { draft, TOOL } from "./drafts";
import type { Session } from "./session";
import { turnEvents } from "./turn";
import type { Halt } from "./types";

// todo_write and its reminder. The list is log state: todos_updated holds the
// complete list, and reduce keeps the latest, so it survives resume, compaction and fork.

const REMINDER_TURNS = 10;

export function writeTodos(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const { call_id } = call.data;
  // Arguments already parsed before authorization; ids must also be unique (rule 24).
  const { todos } = TodoWriteInput.parse(call.data.input);
  const ids = todos.map((t) => t.id);
  const twice = ids.find((id, i) => ids.indexOf(id) !== i);
  if (twice !== undefined)
    return s.append(
      draft.toolResult(
        {
          call_id,
          is_error: true,
          origin: "not_executed",
          preview: `invalid input: duplicate todo id ${twice}`,
        },
        TOOL,
      ),
    );
  return s.append(
    draft.todosUpdated({ call_id, todos }),
    draft.toolResult(
      { call_id, is_error: false, origin: "executed", preview: "ok" },
      TOOL,
    ),
  );
}

/**
 * Right after a turn's input, before its first request: with open items and 10 completed turns
 * since the last todo_write or reminder, show the list once as untrusted reference.
 */
export function todoReminder(s: Session): Halt | undefined {
  const open = s.fold.todos.filter((t) => t.status !== "completed");
  if (open.length === 0) return undefined;
  const turn = turnEvents(s.events, s.fold);
  if (turn.some((e) => e.type === "model_request")) return undefined;
  const since = s.events.findLastIndex(
    (e) =>
      e.type === "todos_updated" ||
      (e.type === "injected" && e.data.source === "todo"),
  );
  const turns = s.events
    .slice(since + 1)
    .filter((e) => e.type === "turn_completed").length;
  // The turn that wrote or reminded completes too; 10 more full turns must follow it.
  if (turns <= REMINDER_TURNS) return undefined;
  return s.append(
    draft.injected({
      source: "todo",
      trust: "untrusted_reference",
      origin: { id: "todos" },
      text: s.fold.todos.map(line).join("\n"),
    }),
  );
}

const line = (t: Todo): string => `- [${t.status}] ${t.content}`;

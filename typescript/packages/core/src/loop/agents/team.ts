import type { EventOf } from "../../fold/state";
import {
  SendMessageInput,
  TeamTaskClaimInput,
  TeamTaskCreateInput,
  TeamTaskUpdateInput,
} from "../../tools/agent-inputs";
import { checkTask } from "../../validate/agents";
import { draft, TOOL } from "../drafts";
import type { Session } from "../session";
import type { Halt, Team } from "../types";

// Teams: the lead's log holds the task list and the mailbox, and members
// change them only through the lead's writer, so claims are atomic per the spec. Every act is
// keyed by the member's call, so a member re-dispatching a call after a restart gets the same
// answer and never claims or sends twice (F7.4).

type Call = EventOf<"tool_call">;
type Answer = ReturnType<Team["act"]>;

const ok = (output: string): Answer => ({ isError: false, output });
const no = (output: string): Answer => ({ isError: true, output });

/** The team whose lead is this session's thread. */
export function teamOf(s: Session): Team {
  return {
    act: (member, call) => act(s, member, call),
    inbox: (member) =>
      s.events.flatMap((e) =>
        e.type === "team_message" &&
        e.data.from !== member &&
        (e.data.to === member || e.data.to === "*")
          ? [e.data]
          : [],
      ),
  };
}

function act(s: Session, member: string, call: Call["data"]): Answer {
  const key = `${member}/${call.call_id}`;
  const { input } = call;
  switch (call.name) {
    case "team_task_create":
      return create(s, key, TeamTaskCreateInput.parse(input));
    case "team_task_claim":
      return claim(s, member, TeamTaskClaimInput.parse(input).task_id);
    case "team_task_update":
      return update(s, member, TeamTaskUpdateInput.parse(input));
    case "send_message":
      return send(s, member, key, SendMessageInput.parse(input));
    default:
      throw new Error(`${call.name} is not a team tool`);
  }
}

function appended(output: string, stopped?: Halt): Answer {
  return stopped === undefined ? ok(output) : no(stopped.message);
}

/** The task id is the creating call's key, so a repeated create is the same task. */
function create(
  s: Session,
  taskId: string,
  input: ReturnType<typeof TeamTaskCreateInput.parse>,
): Answer {
  if (s.fold.tasks.has(taskId)) return ok(taskId);
  const blockers = input.blocked_by ?? [];
  const unknown = blockers.find((id) => !s.fold.tasks.has(id));
  if (unknown !== undefined) return no(`no task ${unknown}`);
  const { subject, description } = input;
  return appended(
    taskId,
    s.append(
      draft.taskCreated({
        task_id: taskId,
        subject,
        blocked_by: blockers,
        ...(description === undefined ? {} : { description }),
      }),
    ),
  );
}

function claim(s: Session, member: string, taskId: string): Answer {
  const task = s.fold.tasks.get(taskId);
  if (task?.status === "claimed" && task.owner === member)
    return ok(`claimed ${taskId}`);
  const data = { task_id: taskId, member };
  const refused = checkTask(s.fold, { type: "team_task_claimed", data });
  if (refused !== undefined) return no(`can't claim: ${refused.message}`);
  return appended(`claimed ${taskId}`, s.append(draft.taskClaimed(data)));
}

function update(
  s: Session,
  member: string,
  input: ReturnType<typeof TeamTaskUpdateInput.parse>,
): Answer {
  const { task_id, status } = input;
  const task = s.fold.tasks.get(task_id);
  if (task?.status === status) return ok(`${task_id} ${status}`);
  if (task?.status !== "claimed" || task.owner !== member)
    return no(`${task_id} is not claimed by ${member}`);
  return appended(
    `${task_id} ${status}`,
    s.append(draft.taskUpdated({ task_id, status })),
  );
}

function send(
  s: Session,
  member: string,
  messageId: string,
  input: ReturnType<typeof SendMessageInput.parse>,
): Answer {
  if (s.fold.messageIds.has(messageId)) return ok("sent");
  return appended(
    "sent",
    s.append(
      draft.teamMessage({
        message_id: messageId,
        from: member,
        to: input.to,
        text: input.text,
      }),
    ),
  );
}

/** A team tool call in this thread: routed to its lead, or to itself as the lead. */
export function teamTool(s: Session, call: Call): Halt | undefined {
  const agents = s.config.agents;
  const answer =
    agents === undefined
      ? no("this agent has no team")
      : (agents.team ?? teamOf(s)).act(agents.name, call.data);
  return s.append(
    draft.toolResult(
      {
        call_id: call.data.call_id,
        is_error: answer.isError,
        origin: "executed",
        preview: answer.output,
      },
      TOOL,
    ),
  );
}

/**
 * Before a turn request: each team message for this agent not yet delivered here, as
 * untrusted reference keyed by its message_id, so a redelivery never injects it twice.
 */
export function deliverMessages(s: Session): Halt | undefined {
  const agents = s.config.agents;
  if (agents === undefined) return undefined;
  const seen = new Set(
    s.events.flatMap((e) =>
      e.type === "injected" && e.data.source === "agent"
        ? [e.data.origin.id]
        : [],
    ),
  );
  const fresh = (agents.team ?? teamOf(s))
    .inbox(agents.name)
    .filter((m) => !seen.has(m.message_id));
  if (fresh.length === 0) return undefined;
  return s.append(
    ...fresh.map((m) =>
      draft.injected({
        source: "agent",
        trust: "untrusted_reference",
        origin: { id: m.message_id },
        text: `${m.from}: ${m.text ?? ""}`,
      }),
    ),
  );
}

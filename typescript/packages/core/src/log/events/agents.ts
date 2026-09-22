import { z } from "zod";
import { ArtifactRef, Budget, Usage } from "../common";
import { type EventDef, event } from "../envelope";
import { CallId, ThreadId } from "../ids";
import { NonEmpty } from "../primitives";
import { type Ruled, withRule } from "../rules";
import type { Arr, EnumOf, Opt, Strict } from "../zod-types";
import { HAS_TEXT_OR_REF } from "./one-of";

// Subagents, handoffs, todos and teams.

const SPAWN_MODES = ["foreground", "background"] as const;
const ISOLATIONS = [
  "none",
  "shared_sandbox",
  "worktree",
  "forked_sandbox",
] as const;
const CHILD_STATUSES = [
  "completed",
  "failed",
  "cancelled",
  "budget_exhausted",
] as const;
const FORWARDED = ["none", "summary", "transcript"] as const;
const TODO_STATUSES = ["pending", "in_progress", "completed"] as const;
const TASK_UPDATES = ["completed", "failed", "released"] as const;

export const AgentSpawnedData: Strict<{
  call_id: typeof CallId;
  child_thread_id: typeof ThreadId;
  agent_name: typeof NonEmpty;
  mode: EnumOf<typeof SPAWN_MODES>;
  isolation: EnumOf<typeof ISOLATIONS>;
  budget: Opt<typeof Budget>;
}> = z.strictObject({
  call_id: CallId,
  child_thread_id: ThreadId,
  agent_name: NonEmpty,
  mode: z.enum(SPAWN_MODES),
  isolation: z.enum(ISOLATIONS),
  budget: Budget.optional(),
});
export const AgentSpawned: EventDef<
  "agent_spawned",
  typeof AgentSpawnedData,
  true
> = event({
  type: "agent_spawned",
  critical: true,
  description:
    "A child thread was created for a spawn call. Durable before the child's thread_started, so cancellation and recovery can find it.",
  data: AgentSpawnedData,
});

export const AgentFinishedData: Strict<{
  child_thread_id: typeof ThreadId;
  status: EnumOf<typeof CHILD_STATUSES>;
  output_ref: Opt<typeof ArtifactRef>;
  usage: typeof Usage;
}> = z.strictObject({
  child_thread_id: ThreadId,
  status: z.enum(CHILD_STATUSES),
  output_ref: ArtifactRef.optional(),
  usage: Usage.describe(
    "The child's aggregate usage; null fields are unknown.",
  ),
});
export const AgentFinished: EventDef<
  "agent_finished",
  typeof AgentFinishedData,
  true
> = event({
  type: "agent_finished",
  critical: true,
  description:
    "The child's one terminal result (F7.5). Exactly once per agent_spawned.",
  data: AgentFinishedData,
});

const HANDOFF_DATA_RULE = {
  if: { properties: { forwarded: { const: "none" } } },
  then: { not: { required: ["forwarded_ref"] } },
  else: { required: ["forwarded_ref"] },
} as const;
export const HandoffData: Ruled<
  Strict<{
    call_id: typeof CallId;
    to_agent: typeof NonEmpty;
    to_thread_id: typeof ThreadId;
    forwarded: EnumOf<typeof FORWARDED>;
    forwarded_ref: Opt<typeof ArtifactRef>;
  }>,
  typeof HANDOFF_DATA_RULE
> = withRule(
  z.strictObject({
    call_id: CallId,
    to_agent: NonEmpty,
    to_thread_id: ThreadId,
    forwarded: z.enum(FORWARDED),
    forwarded_ref: ArtifactRef.optional(),
  }),
  HANDOFF_DATA_RULE,
);
export const Handoff: EventDef<"handoff", typeof HandoffData, true> = event({
  type: "handoff",
  critical: true,
  description:
    "The conversation moves to another agent's new thread. After it this thread takes no new input and no model_request; the host routes the conversation to to_thread_id.",
  data: HandoffData,
});

export const TodosUpdatedData: Strict<{
  call_id: typeof CallId;
  todos: Arr<
    Strict<{
      id: typeof NonEmpty;
      content: typeof NonEmpty;
      status: EnumOf<typeof TODO_STATUSES>;
      active_form: Opt<z.ZodString>;
    }>
  >;
}> = z.strictObject({
  call_id: CallId,
  todos: z.array(
    z.strictObject({
      id: NonEmpty,
      content: NonEmpty,
      status: z.enum(TODO_STATUSES),
      active_form: z.string().optional(),
    }),
  ),
});
export const TodosUpdated: EventDef<
  "todos_updated",
  typeof TodosUpdatedData,
  true
> = event({
  type: "todos_updated",
  critical: true,
  description:
    "The agent's complete todo list after a todo_write call (F8). reduce keeps the latest. ids are unique.",
  data: TodosUpdatedData,
});

export const TeamTaskCreatedData: Strict<{
  task_id: typeof NonEmpty;
  subject: typeof NonEmpty;
  description: Opt<z.ZodString>;
  blocked_by: Arr<typeof NonEmpty>;
}> = z.strictObject({
  task_id: NonEmpty,
  subject: NonEmpty,
  description: z.string().optional(),
  blocked_by: z.array(NonEmpty),
});
export const TeamTaskCreated: EventDef<
  "team_task_created",
  typeof TeamTaskCreatedData,
  true
> = event({
  type: "team_task_created",
  critical: true,
  description:
    "A shared team task, in the team lead's log (the one writer for team state).",
  data: TeamTaskCreatedData,
});

export const TeamTaskClaimedData: Strict<{
  task_id: typeof NonEmpty;
  member: typeof NonEmpty;
}> = z.strictObject({ task_id: NonEmpty, member: NonEmpty });
export const TeamTaskClaimed: EventDef<
  "team_task_claimed",
  typeof TeamTaskClaimedData,
  true
> = event({
  type: "team_task_claimed",
  critical: true,
  description:
    "Atomic claim: the task exists, is open and unclaimed, and every blocker is completed.",
  data: TeamTaskClaimedData,
});

export const TeamTaskUpdatedData: Strict<{
  task_id: typeof NonEmpty;
  status: EnumOf<typeof TASK_UPDATES>;
}> = z.strictObject({
  task_id: NonEmpty,
  status: z.enum(TASK_UPDATES),
});
export const TeamTaskUpdated: EventDef<
  "team_task_updated",
  typeof TeamTaskUpdatedData,
  true
> = event({
  type: "team_task_updated",
  critical: true,
  description:
    "completed and failed close a claimed task; released returns it to the open pool.",
  data: TeamTaskUpdatedData,
});

export const TeamMessageData: Ruled<
  Strict<{
    message_id: typeof NonEmpty;
    from: typeof NonEmpty;
    to: typeof NonEmpty;
    text: Opt<z.ZodString>;
    ref: Opt<typeof ArtifactRef>;
  }>,
  typeof HAS_TEXT_OR_REF
> = withRule(
  z.strictObject({
    message_id: NonEmpty,
    from: NonEmpty,
    to: NonEmpty.describe("A member id, or * for every member."),
    text: z.string().optional(),
    ref: ArtifactRef.optional(),
  }),
  HAS_TEXT_OR_REF,
);
export const TeamMessage: EventDef<
  "team_message",
  typeof TeamMessageData,
  true
> = event({
  type: "team_message",
  critical: true,
  description:
    "An addressed message between members. message_id is unique; delivery into a member's thread is injected{source: agent, origin.id: message_id}, deduplicated by that id.",
  data: TeamMessageData,
});

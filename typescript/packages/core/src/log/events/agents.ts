import { z } from "zod";
import { Actor, ArtifactRef, Budget, Usage } from "../common";
import { type EventSchema, event } from "../envelope";
import { CallId, ThreadId } from "../ids";
import { NonEmpty } from "../primitives";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../zod-types";
import { type TextOrRef, textOrRef } from "./one-of";

// Subagents, handoffs, todos and teams.

const SPAWN_MODES = ["foreground", "background"] as const;
const ISOLATIONS = [
  "none",
  "shared_sandbox",
  "worktree",
  "forked_sandbox",
] as const;
/** Durable before the child's thread_started, so cancellation and recovery can find it. */
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
export const AgentSpawned: EventSchema<
  "agent_spawned",
  typeof AgentSpawnedData,
  true
> = event("agent_spawned", true, Actor, AgentSpawnedData);

const CHILD_STATUSES = [
  "completed",
  "failed",
  "cancelled",
  "budget_exhausted",
] as const;
export const AgentFinishedData: Strict<{
  child_thread_id: typeof ThreadId;
  status: EnumOf<typeof CHILD_STATUSES>;
  output_ref: Opt<typeof ArtifactRef>;
  usage: typeof Usage;
}> = z.strictObject({
  child_thread_id: ThreadId,
  status: z.enum(CHILD_STATUSES),
  output_ref: ArtifactRef.optional(),
  usage: Usage,
});
export const AgentFinished: EventSchema<
  "agent_finished",
  typeof AgentFinishedData,
  true
> = event("agent_finished", true, Actor, AgentFinishedData);

type HandoffShape = {
  call_id: typeof CallId;
  to_agent: typeof NonEmpty;
  to_thread_id: typeof ThreadId;
};
const handoffShape: HandoffShape = {
  call_id: CallId,
  to_agent: NonEmpty,
  to_thread_id: ThreadId,
};
const FORWARDED = ["summary", "transcript"] as const;
// forwarded_ref is present exactly when something is forwarded.
export const HandoffData: z.ZodDiscriminatedUnion<
  [
    Strict<HandoffShape & { forwarded: Lit<"none"> }>,
    Strict<
      HandoffShape & {
        forwarded: EnumOf<typeof FORWARDED>;
        forwarded_ref: typeof ArtifactRef;
      }
    >,
  ],
  "forwarded"
> = z.discriminatedUnion("forwarded", [
  z.strictObject({ ...handoffShape, forwarded: z.literal("none") }),
  z.strictObject({
    ...handoffShape,
    forwarded: z.enum(FORWARDED),
    forwarded_ref: ArtifactRef,
  }),
]);
export const Handoff: EventSchema<"handoff", typeof HandoffData, true> = event(
  "handoff",
  true,
  Actor,
  HandoffData,
);

const TODO_STATUSES = ["pending", "in_progress", "completed"] as const;
export const Todo: Strict<{
  id: typeof NonEmpty;
  content: typeof NonEmpty;
  status: EnumOf<typeof TODO_STATUSES>;
  active_form: Opt<z.ZodString>;
}> = z.strictObject({
  id: NonEmpty,
  content: NonEmpty,
  status: z.enum(TODO_STATUSES),
  active_form: z.string().optional(),
});
/** The agent's complete todo list after a todo_write call. */
export const TodosUpdatedData: Strict<{
  call_id: typeof CallId;
  todos: Arr<typeof Todo>;
}> = z.strictObject({
  call_id: CallId,
  todos: z.array(Todo),
});
export const TodosUpdated: EventSchema<
  "todos_updated",
  typeof TodosUpdatedData,
  true
> = event("todos_updated", true, Actor, TodosUpdatedData);

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
export const TeamTaskCreated: EventSchema<
  "team_task_created",
  typeof TeamTaskCreatedData,
  true
> = event("team_task_created", true, Actor, TeamTaskCreatedData);

export const TeamTaskClaimedData: Strict<{
  task_id: typeof NonEmpty;
  member: typeof NonEmpty;
}> = z.strictObject({
  task_id: NonEmpty,
  member: NonEmpty,
});
export const TeamTaskClaimed: EventSchema<
  "team_task_claimed",
  typeof TeamTaskClaimedData,
  true
> = event("team_task_claimed", true, Actor, TeamTaskClaimedData);

const TASK_UPDATES = ["completed", "failed", "released"] as const;
export const TeamTaskUpdatedData: Strict<{
  task_id: typeof NonEmpty;
  status: EnumOf<typeof TASK_UPDATES>;
}> = z.strictObject({ task_id: NonEmpty, status: z.enum(TASK_UPDATES) });
export const TeamTaskUpdated: EventSchema<
  "team_task_updated",
  typeof TeamTaskUpdatedData,
  true
> = event("team_task_updated", true, Actor, TeamTaskUpdatedData);

type TeamMessageShape = {
  message_id: typeof NonEmpty;
  from: typeof NonEmpty;
  to: typeof NonEmpty;
};
/** `to` is a member id, or `*` for every member. */
export const TeamMessageData: TextOrRef<TeamMessageShape> = textOrRef({
  message_id: NonEmpty,
  from: NonEmpty,
  to: NonEmpty,
});
export const TeamMessage: EventSchema<
  "team_message",
  typeof TeamMessageData,
  true
> = event("team_message", true, Actor, TeamMessageData);

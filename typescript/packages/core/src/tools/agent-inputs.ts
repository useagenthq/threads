import { z } from "zod";
import type { Arr, EnumOf, Opt, Strict } from "../log/zod-types";

// Inputs of the framework tools for subagents, handoffs, teams and todos. They are
// catalog entries like the sandbox tools; the loop runs them, since they append to the log.

const Text: z.ZodString = z.string().min(1);

const TODO_STATUSES = ["pending", "in_progress", "completed"] as const;
const ISOLATIONS = [
  "none",
  "shared_sandbox",
  "worktree",
  "forked_sandbox",
] as const;
const TASK_UPDATES = ["completed", "failed", "released"] as const;

type TodoItem = Strict<{
  id: z.ZodString;
  content: z.ZodString;
  status: EnumOf<typeof TODO_STATUSES>;
  active_form: Opt<z.ZodString>;
}>;

/** The wire todo shape (todos_updated.todos). Unique ids are semantic rule 24. */
export const TodoWriteInput: Strict<{ todos: Arr<TodoItem> }> = z.strictObject({
  todos: z
    .array(
      z.strictObject({
        id: Text,
        content: Text,
        status: z.enum(TODO_STATUSES),
        active_form: z
          .string()
          .optional()
          .describe("Present tense, shown while in_progress."),
      }),
    )
    .describe("The complete new list; ids are unique."),
});

export const SpawnAgentInput: Strict<{
  agent: z.ZodString;
  prompt: z.ZodString;
  background: Opt<z.ZodBoolean>;
  isolation: Opt<EnumOf<typeof ISOLATIONS>>;
}> = z.strictObject({
  agent: Text.describe("A subagent name listed in your instructions."),
  prompt: Text.describe("The task; the subagent sees nothing else."),
  background: z
    .boolean()
    .optional()
    .describe("Default false: wait for the result."),
  isolation: z.enum(ISOLATIONS).optional().describe("Default none."),
});

export const HandoffInput: Strict<{ agent: z.ZodString }> = z.strictObject({
  agent: Text.describe("A handoff target listed in your instructions."),
});

export const SendMessageInput: Strict<{
  to: z.ZodString;
  text: z.ZodString;
}> = z.strictObject({
  to: Text.describe("A member's agent name, or * for every member."),
  text: Text,
});

export const TeamTaskCreateInput: Strict<{
  subject: z.ZodString;
  description: Opt<z.ZodString>;
  blocked_by: Opt<Arr<z.ZodString>>;
}> = z.strictObject({
  subject: Text,
  description: z.string().optional(),
  blocked_by: z
    .array(Text)
    .optional()
    .describe("Task ids that must complete before this one can be claimed."),
});

export const TeamTaskClaimInput: Strict<{ task_id: z.ZodString }> =
  z.strictObject({ task_id: Text });

export const TeamTaskUpdateInput: Strict<{
  task_id: z.ZodString;
  status: EnumOf<typeof TASK_UPDATES>;
}> = z.strictObject({
  task_id: Text,
  status: z
    .enum(TASK_UPDATES)
    .describe("released returns a claimed task to the open pool."),
});

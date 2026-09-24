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

/**
 * ask_user. Options unique under the matching rule, and free of the reply separators when
 * multi_select, are semantic rule 48 (spec/schema/README.md, "Questions and remembered rules").
 */
export const AskUserInput: Strict<{
  question: z.ZodString;
  options: Opt<Arr<z.ZodString>>;
  multi_select: Opt<z.ZodBoolean>;
}> = z.strictObject({
  question: Text,
  options: z
    .array(Text)
    .min(2)
    .optional()
    .describe(
      "The only answers accepted, distinct ignoring case and surrounding spaces. Include an escape option such as Other when the list may not cover every answer. Omit for a free-text answer.",
    ),
  multi_select: z
    .boolean()
    .optional()
    .describe(
      "Default false. With options: the user may pick several; no option may contain a comma or a line break.",
    ),
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

/**
 * tool_search: at most 200 characters, counted in code points by the tool itself (a JSON Schema
 * maxLength would count UTF-16 units in Zod and code points in Python).
 */
export const ToolSearchInput: Strict<{
  query: z.ZodString;
  limit: z.ZodDefault<z.ZodInt>;
}> = z.strictObject({
  query: Text.describe(
    "Exact tool names separated by commas, or keywords. At most 200 characters.",
  ),
  limit: z
    .int()
    .min(1)
    .max(10)
    .default(5)
    .describe("How many keyword matches to load."),
});

import { z } from "zod";
import type { Arr, Strict } from "../log/zod-types";
import type { CatalogEntry } from "./catalog";

// The model tools of a team (spec/schema/README.md, "Teams"). A member is addressed by its
// name; the tool binds the name to the generation the caller's own log last recorded for it.
// Each call's tool_result.preview is its result as RFC 8785 JSON.

const Text: z.ZodString = z.string().min(1);
const Member: z.ZodString = Text.describe(
  "A member's name, such as researcher-1.",
);

export const StartInput: Strict<{ agent: z.ZodString; task: z.ZodString }> =
  z.strictObject({
    agent: Text.describe("An agent your team lists."),
    task: Text.describe("The member's first input; it sees nothing else."),
  });

export const SendInput: Strict<{ to: z.ZodString; text: z.ZodString }> =
  z.strictObject({ to: Member, text: Text });

export const AskInput: Strict<{ to: z.ZodString; question: z.ZodString }> =
  z.strictObject({ to: Member, question: Text });

export const ReplyInput: Strict<{ ask_id: z.ZodString; text: z.ZodString }> =
  z.strictObject({
    ask_id: Text.describe("The ask_id of an ask you received."),
    text: Text,
  });

export const WaitInput: Strict<{ members: Arr<z.ZodString> }> = z.strictObject({
  members: z.array(Member).min(1),
});

export const MonitorInput: Strict<{ member: z.ZodString }> = z.strictObject({
  member: Member,
});

export const CancelInput: Strict<{ member: z.ZodString }> = z.strictObject({
  member: Member,
});

export const TEAM_ENTRIES: readonly CatalogEntry[] = [
  {
    name: "ask",
    description:
      "Ask a team member a question. Its reply is this call's result; you wait for it.",
    input: AskInput,
  },
  {
    name: "cancel",
    description:
      "Cancel a member you started. It stops at its next step and ends cancelled.",
    input: CancelInput,
  },
  {
    name: "monitor",
    description:
      "Get a message when a member ends, with its result. An idle watcher wakes for it.",
    input: MonitorInput,
  },
  {
    name: "reply",
    description: "Answer an ask you received, by its ask_id.",
    input: ReplyInput,
  },
  {
    name: "send",
    description:
      "Send a message to a team member. It arrives as the member's next input and wakes it if idle.",
    input: SendInput,
  },
  {
    name: "start",
    description:
      "Start a team member from an agent your team lists. Returns its name; when it finishes, its result arrives as a message.",
    input: StartInput,
  },
  {
    name: "wait",
    description:
      "Wait until each listed member is idle or has ended, or the deadline passes. Returns their results.",
    input: WaitInput,
  },
];

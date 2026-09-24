import { JsonValue } from "@threads/core/host";
import { z } from "zod";
import { CHAT_KEY } from "./key";

// The UI routes' request bodies (spec/schema/host-api/host-api.v1.schema.json AiSdkChatRequest,
// AgUiRunInput): only the part threads reads is parsed. The stock clients send more, which is
// ignored, never read as truth: the log is.

type Loose<S extends z.core.$ZodLooseShape> = z.ZodObject<S, z.core.$loose>;
type Opt<T extends z.core.SomeType> = z.ZodOptional<T>;

const ChatKey: z.ZodString = z.string().regex(CHAT_KEY);

/** A message id threads records as user_input.client_message_id. */
export const ClientMessageId: z.ZodString = z
  .string()
  .regex(/^[A-Za-z0-9_.-]{1,128}$/);

const AiSdkApproval: Loose<{
  id: z.ZodString;
  approved: Opt<z.ZodBoolean>;
  reason: Opt<z.ZodString>;
}> = z.looseObject({
  id: z.string(),
  approved: z.boolean().optional(),
  reason: z.string().optional(),
});

const AiSdkPart: Loose<{
  type: z.ZodString;
  text: Opt<z.ZodString>;
  toolCallId: Opt<z.ZodString>;
  state: Opt<z.ZodString>;
  approval: Opt<typeof AiSdkApproval>;
  output: Opt<typeof JsonValue>;
}> = z.looseObject({
  type: z.string(),
  text: z.string().optional(),
  toolCallId: z.string().optional(),
  state: z.string().optional(),
  approval: AiSdkApproval.optional(),
  output: JsonValue.optional(),
});
export type AiSdkPart = z.infer<typeof AiSdkPart>;

const AiSdkMessage: Loose<{
  id: z.ZodString;
  role: z.ZodEnum<{ system: "system"; user: "user"; assistant: "assistant" }>;
  parts: z.ZodArray<typeof AiSdkPart>;
}> = z.looseObject({
  id: z.string(),
  role: z.enum(["system", "user", "assistant"]),
  parts: z.array(AiSdkPart),
});

export const AiSdkChatRequest: Loose<{
  id: z.ZodString;
  messages: z.ZodArray<typeof AiSdkMessage>;
  trigger: z.ZodEnum<{
    "submit-message": "submit-message";
    "regenerate-message": "regenerate-message";
  }>;
  messageId: Opt<z.ZodString>;
}> = z.looseObject({
  id: ChatKey,
  messages: z.array(AiSdkMessage).min(1),
  trigger: z.enum(["submit-message", "regenerate-message"]),
  messageId: z.string().optional(),
});
export type AiSdkChatRequest = z.infer<typeof AiSdkChatRequest>;

const AgUiContentPart: Loose<{ type: z.ZodString; text: Opt<z.ZodString> }> =
  z.looseObject({ type: z.string(), text: z.string().optional() });

const AgUiMessage: Loose<{
  id: z.ZodString;
  role: z.ZodString;
  content: Opt<
    z.ZodUnion<readonly [z.ZodString, z.ZodArray<typeof AgUiContentPart>]>
  >;
}> = z.looseObject({
  id: z.string(),
  role: z.string(),
  content: z.union([z.string(), z.array(AgUiContentPart)]).optional(),
});
export type AgUiMessage = z.infer<typeof AgUiMessage>;

const AgUiResumeEntry: Loose<{
  interruptId: z.ZodString;
  status: z.ZodEnum<{ resolved: "resolved"; cancelled: "cancelled" }>;
  payload: Opt<typeof JsonValue>;
}> = z.looseObject({
  interruptId: z.string(),
  status: z.enum(["resolved", "cancelled"]),
  payload: JsonValue.optional(),
});
export type AgUiResumeEntry = z.infer<typeof AgUiResumeEntry>;

export const AgUiRunInput: Loose<{
  threadId: z.ZodString;
  runId: z.ZodString;
  messages: z.ZodArray<typeof AgUiMessage>;
  tools: Opt<z.ZodArray<z.ZodRecord<z.ZodString, z.ZodUnknown>>>;
  resume: Opt<z.ZodArray<typeof AgUiResumeEntry>>;
}> = z.looseObject({
  threadId: ChatKey,
  runId: z.string(),
  messages: z.array(AgUiMessage),
  tools: z.array(z.record(z.string(), z.unknown())).optional(),
  resume: z.array(AgUiResumeEntry).optional(),
});
export type AgUiRunInput = z.infer<typeof AgUiRunInput>;

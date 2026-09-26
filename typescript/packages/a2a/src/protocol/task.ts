import { JsonObject, JsonValue } from "@threads/core/adapter";
import { z } from "zod";
import type { Arr, EnumOf, Opt, Strict } from "./zod";

// The A2A data model as JSON: `lf.a2a.v1` messages under the protobuf JSON mapping, so every
// field is lowerCamelCase and every enum is its `TASK_STATE_`-style name. Vendored proto:
// spec/schema/a2a/a2a.proto at tag v1.0.1. Every byte that crosses our boundary is parsed with
// these, in both directions, so a peer's extra field is refused rather than stored as ours.
//
// `metadata` and `extensions` are the only places the proto lets unknown data through, and they
// are the only places we keep it (as provider data). Everything else is a strict object.

const TASK_STATES = [
  "TASK_STATE_SUBMITTED",
  "TASK_STATE_WORKING",
  "TASK_STATE_COMPLETED",
  "TASK_STATE_FAILED",
  "TASK_STATE_CANCELED",
  "TASK_STATE_INPUT_REQUIRED",
  "TASK_STATE_REJECTED",
  "TASK_STATE_AUTH_REQUIRED",
] as const;

/**
 * The eight states we accept. `TASK_STATE_UNSPECIFIED` is the proto's zero value and means
 * "unknown or indeterminate", which is never an answer we can act on, so it is a parse error.
 */
export const TaskState: EnumOf<typeof TASK_STATES> = z.enum(TASK_STATES);
export type TaskState = z.infer<typeof TaskState>;

const TERMINAL = new Set<TaskState>([
  "TASK_STATE_COMPLETED",
  "TASK_STATE_FAILED",
  "TASK_STATE_CANCELED",
  "TASK_STATE_REJECTED",
]);
const INTERRUPTED = new Set<TaskState>([
  "TASK_STATE_INPUT_REQUIRED",
  "TASK_STATE_AUTH_REQUIRED",
]);

/** Terminal per the pinned spec: completed, failed, canceled, rejected. */
export function isTerminal(state: TaskState): boolean {
  return TERMINAL.has(state);
}

/** Interrupted per the pinned spec: input-required, auth-required. */
export function isInterrupted(state: TaskState): boolean {
  return INTERRUPTED.has(state);
}

/** A stream closes, and a follow stops, once the task can go no further on its own. */
export function isSettled(state: TaskState): boolean {
  return TERMINAL.has(state) || INTERRUPTED.has(state);
}

const ROLES = ["ROLE_UNSPECIFIED", "ROLE_USER", "ROLE_AGENT"] as const;
export const Role: EnumOf<typeof ROLES> = z.enum(ROLES);
export type Role = z.infer<typeof Role>;

/**
 * One `oneof content` member plus the shared fields. The proto's oneof is not tagged in JSON: the
 * set field's own name is the key, so a part is an object with at most one of text, raw, url and
 * data. We read them all and refuse file parts a layer up, with ContentTypeNotSupportedError,
 * rather than failing the parse: a peer that sends a file deserves the protocol's own answer.
 */
export const Part: Strict<{
  text: Opt<z.ZodString>;
  raw: Opt<z.ZodString>;
  url: Opt<z.ZodString>;
  data: Opt<typeof JsonValue>;
  metadata: Opt<typeof JsonObject>;
  filename: Opt<z.ZodString>;
  mediaType: Opt<z.ZodString>;
}> = z.strictObject({
  text: z.string().optional(),
  raw: z.string().optional(),
  url: z.string().optional(),
  data: JsonValue.optional(),
  metadata: JsonObject.optional(),
  filename: z.string().optional(),
  mediaType: z.string().optional(),
});
export type Part = z.infer<typeof Part>;

export const Message: Strict<{
  messageId: z.ZodString;
  contextId: Opt<z.ZodString>;
  taskId: Opt<z.ZodString>;
  role: typeof Role;
  parts: Arr<typeof Part>;
  metadata: Opt<typeof JsonObject>;
  extensions: Opt<Arr<z.ZodString>>;
  referenceTaskIds: Opt<Arr<z.ZodString>>;
}> = z.strictObject({
  messageId: z.string().min(1),
  contextId: z.string().optional(),
  taskId: z.string().optional(),
  role: Role,
  parts: z.array(Part),
  metadata: JsonObject.optional(),
  extensions: z.array(z.string()).optional(),
  referenceTaskIds: z.array(z.string()).optional(),
});
export type Message = z.infer<typeof Message>;

export const Artifact: Strict<{
  artifactId: z.ZodString;
  name: Opt<z.ZodString>;
  description: Opt<z.ZodString>;
  parts: Arr<typeof Part>;
  metadata: Opt<typeof JsonObject>;
  extensions: Opt<Arr<z.ZodString>>;
}> = z.strictObject({
  artifactId: z.string().min(1),
  name: z.string().optional(),
  description: z.string().optional(),
  parts: z.array(Part),
  metadata: JsonObject.optional(),
  extensions: z.array(z.string()).optional(),
});
export type Artifact = z.infer<typeof Artifact>;

export const TaskStatus: Strict<{
  state: typeof TaskState;
  message: Opt<typeof Message>;
  timestamp: Opt<z.ZodString>;
}> = z.strictObject({
  state: TaskState,
  message: Message.optional(),
  // google.protobuf.Timestamp in JSON: RFC 3339 with a Z or an offset.
  timestamp: z.string().optional(),
});
export type TaskStatus = z.infer<typeof TaskStatus>;

export const Task: Strict<{
  id: z.ZodString;
  contextId: Opt<z.ZodString>;
  status: typeof TaskStatus;
  artifacts: Opt<Arr<typeof Artifact>>;
  history: Opt<Arr<typeof Message>>;
  metadata: Opt<typeof JsonObject>;
}> = z.strictObject({
  id: z.string().min(1),
  contextId: z.string().optional(),
  status: TaskStatus,
  artifacts: z.array(Artifact).optional(),
  history: z.array(Message).optional(),
  metadata: JsonObject.optional(),
});
export type Task = z.infer<typeof Task>;

export const TaskStatusUpdateEvent: Strict<{
  taskId: z.ZodString;
  contextId: z.ZodString;
  status: typeof TaskStatus;
  metadata: Opt<typeof JsonObject>;
}> = z.strictObject({
  taskId: z.string().min(1),
  contextId: z.string().min(1),
  status: TaskStatus,
  metadata: JsonObject.optional(),
});
export type TaskStatusUpdateEvent = z.infer<typeof TaskStatusUpdateEvent>;

export const TaskArtifactUpdateEvent: Strict<{
  taskId: z.ZodString;
  contextId: z.ZodString;
  artifact: typeof Artifact;
  append: Opt<z.ZodBoolean>;
  lastChunk: Opt<z.ZodBoolean>;
  metadata: Opt<typeof JsonObject>;
}> = z.strictObject({
  taskId: z.string().min(1),
  contextId: z.string().min(1),
  artifact: Artifact,
  append: z.boolean().optional(),
  lastChunk: z.boolean().optional(),
  metadata: JsonObject.optional(),
});
export type TaskArtifactUpdateEvent = z.infer<typeof TaskArtifactUpdateEvent>;

/**
 * `SendMessageResponse`: the `oneof payload`. A oneof is one key in JSON, and "exactly one" is a
 * rule JSON Schema would need `oneOf` for, so `payloadOf` decides it where the response is read.
 */
export const SendMessageResponse: Strict<{
  task: Opt<typeof Task>;
  message: Opt<typeof Message>;
}> = z.strictObject({ task: Task.optional(), message: Message.optional() });
export type SendMessageResponse = z.infer<typeof SendMessageResponse>;

/** The one payload a response or stream item set, or undefined when none or several are set. */
export function payloadOf(
  response: SendMessageResponse,
): { readonly task: Task } | { readonly message: Message } | undefined {
  if (response.task !== undefined && response.message === undefined)
    return { task: response.task };
  if (response.message !== undefined && response.task === undefined)
    return { message: response.message };
  return undefined;
}

/** `StreamResponse`: the `oneof payload` of a streaming operation's SSE items. */
export const StreamResponse: Strict<{
  task: Opt<typeof Task>;
  message: Opt<typeof Message>;
  statusUpdate: Opt<typeof TaskStatusUpdateEvent>;
  artifactUpdate: Opt<typeof TaskArtifactUpdateEvent>;
}> = z.strictObject({
  task: Task.optional(),
  message: Message.optional(),
  statusUpdate: TaskStatusUpdateEvent.optional(),
  artifactUpdate: TaskArtifactUpdateEvent.optional(),
});
export type StreamResponse = z.infer<typeof StreamResponse>;

export type StreamPayload =
  | { readonly kind: "task"; readonly task: Task }
  | { readonly kind: "message"; readonly message: Message }
  | { readonly kind: "status"; readonly status: TaskStatusUpdateEvent }
  | { readonly kind: "artifact"; readonly artifact: TaskArtifactUpdateEvent };

/** The one payload a stream item set, or undefined when it set none or several. */
export function streamPayload(item: StreamResponse): StreamPayload | undefined {
  const set = [
    item.task === undefined
      ? undefined
      : ({ kind: "task", task: item.task } as const),
    item.message === undefined
      ? undefined
      : ({ kind: "message", message: item.message } as const),
    item.statusUpdate === undefined
      ? undefined
      : ({ kind: "status", status: item.statusUpdate } as const),
    item.artifactUpdate === undefined
      ? undefined
      : ({ kind: "artifact", artifact: item.artifactUpdate } as const),
  ].flatMap((p) => p ?? []);
  return set.length === 1 ? set[0] : undefined;
}

/** The text of every text part, joined: what a model is shown of a remote's answer. */
export function textOf(parts: readonly Part[]): string {
  return parts.flatMap((p) => p.text ?? []).join("");
}

/** A part the cut allows: text, or structured data. A file part is refused where it arrives. */
export function isFilePart(part: Part): boolean {
  return part.raw !== undefined || part.url !== undefined;
}

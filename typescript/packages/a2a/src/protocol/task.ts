import { JsonObject, JsonValue } from "@threads/core/adapter";
import { z } from "zod";
import type { Arr, EnumOf, Opt, Strict, Union } from "./zod";

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

const ROLES = ["ROLE_USER", "ROLE_AGENT"] as const;

/**
 * The two roles we accept. `ROLE_UNSPECIFIED` is the proto's zero value and says nothing about who
 * sent a message, which is never something we can act on, so it is a parse error — the same reason
 * `TASK_STATE_UNSPECIFIED` is one.
 */
export const Role: EnumOf<typeof ROLES> = z.enum(ROLES);
export type Role = z.infer<typeof Role>;

/**
 * ProtoJSON writes `bytes` as base64. Either alphabet and padded or not, which is what the protobuf
 * JSON mapping says a parser accepts; the length is checked too, so a string that is simply not
 * base64 is refused where it arrives rather than becoming bytes nobody can decode.
 */
const Base64: z.ZodString = z
  .string()
  .regex(
    /^(?:[A-Za-z0-9+/\-_]{4})*(?:[A-Za-z0-9+/\-_]{2}(?:==)?|[A-Za-z0-9+/\-_]{3}=?)?$/,
    "bytes are base64 under the protobuf JSON mapping",
  );

/** The fields a part carries whichever content member it set. */
type PartShared = {
  metadata: Opt<typeof JsonObject>;
  filename: Opt<z.ZodString>;
  mediaType: Opt<z.ZodString>;
};

const PART_SHARED: PartShared = {
  metadata: JsonObject.optional(),
  filename: z.string().optional(),
  mediaType: z.string().optional(),
};

const TextPart: Strict<PartShared & { text: z.ZodString }> = z.strictObject({
  text: z.string(),
  ...PART_SHARED,
});
const RawPart: Strict<PartShared & { raw: z.ZodString }> = z.strictObject({
  raw: Base64,
  ...PART_SHARED,
});
const UrlPart: Strict<PartShared & { url: z.ZodString }> = z.strictObject({
  url: z.string(),
  ...PART_SHARED,
});
const DataPart: Strict<PartShared & { data: typeof JsonValue }> =
  z.strictObject({ data: JsonValue, ...PART_SHARED });

/**
 * The proto's `oneof content`, as the four things it can be. A oneof is not tagged in JSON — the set
 * field's own name is the key — and "exactly one" is the whole of what a oneof means, so each member
 * is its own strict object and the union is what enforces it. One object with four optional fields
 * could not: `{}` would parse as a part carrying nothing and read back as empty text, and
 * `{text, raw}` would parse as a part that is two things at once.
 *
 * A file part still parses. It is refused a layer up with ContentTypeNotSupportedError, because a
 * peer that sends one deserves the protocol's own answer rather than a parse error.
 */
export const Part: Union<
  [typeof TextPart, typeof RawPart, typeof UrlPart, typeof DataPart]
> = z.union([TextPart, RawPart, UrlPart, DataPart]);
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
  // REQUIRED in the proto, which for a repeated field means at least one: an empty list is a
  // message with no content, and the proto's own comment on Artifact.parts says so outright.
  parts: z.array(Part).min(1),
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
  parts: z.array(Part).min(1),
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
  // google.protobuf.Timestamp in JSON: RFC 3339 with a Z or an offset, which is checked rather
  // than described. The pattern is a shape, not a calendar: no JSON Schema can rule out month 13,
  // and both languages read this one schema, so both accept exactly the same strings.
  timestamp: z
    .string()
    .regex(
      /^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$/,
      "a timestamp is RFC 3339",
    )
    .optional(),
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
  return parts.flatMap((p) => ("text" in p ? p.text : [])).join("");
}

/** A part the cut allows: text, or structured data. A file part is refused where it arrives. */
export function isFilePart(part: Part): boolean {
  return "raw" in part || "url" in part;
}

import { JsonObject } from "@threads/core/adapter";
import { z } from "zod";
import { Message, Task, TaskState } from "./task";
import type { Arr, Opt, Strict } from "./zod";

// The request and list-response messages of the six operations we use. Push-notification config
// and the extended card are not supported, so their requests have no schema here: an inbound call
// to one answers UnsupportedOperationError / PushNotificationNotSupportedError by method name.
//
// `tenant` is on every request because the proto puts it there. We serve no `/{tenant}/…` path and
// declare no `AgentInterface.tenant`, so a request that sets it must set it to the authenticated
// principal's tenant; anything else is InvalidParams. That check is in the route, not the schema.

export const SendMessageConfiguration: Strict<{
  acceptedOutputModes: Opt<Arr<z.ZodString>>;
  taskPushNotificationConfig: Opt<typeof JsonObject>;
  historyLength: Opt<z.ZodNumber>;
  returnImmediately: Opt<z.ZodBoolean>;
}> = z.strictObject({
  acceptedOutputModes: z.array(z.string()).optional(),
  // Parsed, then refused: PushNotificationNotSupportedError, with our own card saying so.
  taskPushNotificationConfig: JsonObject.optional(),
  historyLength: z.number().int().optional(),
  returnImmediately: z.boolean().optional(),
});
export type SendMessageConfiguration = z.infer<typeof SendMessageConfiguration>;

export const SendMessageRequest: Strict<{
  tenant: Opt<z.ZodString>;
  message: typeof Message;
  configuration: Opt<typeof SendMessageConfiguration>;
  metadata: Opt<typeof JsonObject>;
}> = z.strictObject({
  tenant: z.string().optional(),
  message: Message,
  configuration: SendMessageConfiguration.optional(),
  metadata: JsonObject.optional(),
});
export type SendMessageRequest = z.infer<typeof SendMessageRequest>;

export const GetTaskRequest: Strict<{
  tenant: Opt<z.ZodString>;
  id: z.ZodString;
  historyLength: Opt<z.ZodNumber>;
}> = z.strictObject({
  tenant: z.string().optional(),
  id: z.string().min(1),
  historyLength: z.number().int().optional(),
});
export type GetTaskRequest = z.infer<typeof GetTaskRequest>;

export const ListTasksRequest: Strict<{
  tenant: Opt<z.ZodString>;
  contextId: Opt<z.ZodString>;
  status: Opt<typeof TaskState>;
  pageSize: Opt<z.ZodNumber>;
  pageToken: Opt<z.ZodString>;
  historyLength: Opt<z.ZodNumber>;
  statusTimestampAfter: Opt<z.ZodString>;
  includeArtifacts: Opt<z.ZodBoolean>;
}> = z.strictObject({
  tenant: z.string().optional(),
  contextId: z.string().optional(),
  status: TaskState.optional(),
  pageSize: z.number().int().optional(),
  pageToken: z.string().optional(),
  historyLength: z.number().int().optional(),
  statusTimestampAfter: z.string().optional(),
  includeArtifacts: z.boolean().optional(),
});
export type ListTasksRequest = z.infer<typeof ListTasksRequest>;

export const ListTasksResponse: Strict<{
  tasks: Arr<typeof Task>;
  nextPageToken: z.ZodString;
  pageSize: z.ZodNumber;
  totalSize: z.ZodNumber;
}> = z.strictObject({
  tasks: z.array(Task),
  nextPageToken: z.string(),
  pageSize: z.number().int(),
  totalSize: z.number().int(),
});
export type ListTasksResponse = z.infer<typeof ListTasksResponse>;

export const CancelTaskRequest: Strict<{
  tenant: Opt<z.ZodString>;
  id: z.ZodString;
  metadata: Opt<typeof JsonObject>;
}> = z.strictObject({
  tenant: z.string().optional(),
  id: z.string().min(1),
  metadata: JsonObject.optional(),
});
export type CancelTaskRequest = z.infer<typeof CancelTaskRequest>;

export const SubscribeToTaskRequest: Strict<{
  tenant: Opt<z.ZodString>;
  id: z.ZodString;
}> = z.strictObject({
  tenant: z.string().optional(),
  id: z.string().min(1),
});
export type SubscribeToTaskRequest = z.infer<typeof SubscribeToTaskRequest>;

/** The default `ListTasks` page, and its ceiling, from the proto's own comment on `page_size`. */
export const DEFAULT_PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 100;

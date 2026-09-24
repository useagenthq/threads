import { z } from "zod";
import { NonEmpty, Uuid } from "./primitives";
import type { Brand } from "./zod-types";

// An id with its own `.meta` id is its own def in the export; the others are brands of their
// wire shape, which the schema names directly (Uuid, NonEmpty).

export const ThreadId: Brand<z.ZodString, "ThreadId"> = Uuid.meta({
  id: "ThreadId",
}).brand<"ThreadId">();
export type ThreadId = z.infer<typeof ThreadId>;

export const BranchId: Brand<z.ZodString, "BranchId"> = Uuid.meta({
  id: "BranchId",
}).brand<"BranchId">();
export type BranchId = z.infer<typeof BranchId>;

export const EventId: Brand<z.ZodString, "EventId"> = Uuid.meta({
  id: "EventId",
}).brand<"EventId">();
export type EventId = z.infer<typeof EventId>;

export const ChallengeId: Brand<z.ZodString, "ChallengeId"> =
  Uuid.brand<"ChallengeId">();
export type ChallengeId = z.infer<typeof ChallengeId>;

export const CallId: Brand<z.ZodString, "CallId"> = z
  .string()
  .regex(/^[A-Za-z0-9_.-]{1,128}$/)
  .meta({
    id: "CallId",
    description:
      "Tool call id; may be provider-issued. The effect key is derived: <branch_id of the tool_call event>:<call_id>.",
  })
  .brand<"CallId">();
export type CallId = z.infer<typeof CallId>;

export const SandboxId: Brand<z.ZodString, "SandboxId"> =
  NonEmpty.brand<"SandboxId">();
export type SandboxId = z.infer<typeof SandboxId>;

export const SnapshotId: Brand<z.ZodString, "SnapshotId"> =
  NonEmpty.brand<"SnapshotId">();
export type SnapshotId = z.infer<typeof SnapshotId>;

export const OccurrenceId: Brand<z.ZodString, "OccurrenceId"> =
  NonEmpty.brand<"OccurrenceId">();
export type OccurrenceId = z.infer<typeof OccurrenceId>;

// Teams (spec/schema/README.md, "Teams"). Mail, ask, wait and monitor ids are derived from
// logged bytes, so a re-dispatched call names the same row and a fold rebuilds the same ids.

export const TeamId: Brand<z.ZodString, "TeamId"> = Uuid.meta({
  id: "TeamId",
}).brand<"TeamId">();
export type TeamId = z.infer<typeof TeamId>;

export const RequestId: Brand<z.ZodString, "RequestId"> = Uuid.meta({
  id: "RequestId",
  description:
    "One operator request: a team.start, send, ask, wait or cancel call.",
}).brand<"RequestId">();
export type RequestId = z.infer<typeof RequestId>;

export const MemberName: Brand<z.ZodString, "MemberName"> = z
  .string()
  .regex(/^[a-z][a-z0-9_]{0,63}(-[1-9][0-9]*)?$/)
  .meta({
    id: "MemberName",
    description:
      "<agent>-<k>, k counting that agent's members in the team from 1. The lead's name is its agent name.",
  })
  .brand<"MemberName">();
export type MemberName = z.infer<typeof MemberName>;

export const MailId: Brand<z.ZodString, "MailId"> = NonEmpty.meta({
  id: "MailId",
  description:
    "<sender branch_id>:<call_id or request_id> for mail a call sends; <sender branch_id>:<event_id of the message_sent> for mail the runtime sends (notifications, bounces, lead-close cancels).",
}).brand<"MailId">();
export type MailId = z.infer<typeof MailId>;

export const AskId: Brand<z.ZodString, "AskId"> = NonEmpty.meta({
  id: "AskId",
  description: "The MailId of the ask.",
}).brand<"AskId">();
export type AskId = z.infer<typeof AskId>;

export const MonitorId: Brand<z.ZodString, "MonitorId"> = NonEmpty.meta({
  id: "MonitorId",
  description:
    "<watcher branch_id>:<event_id of the registering event>:<target name>; the target name is task for a start's task monitor. Derived, never generated.",
}).brand<"MonitorId">();
export type MonitorId = z.infer<typeof MonitorId>;

export const WaitId: Brand<z.ZodString, "WaitId"> = NonEmpty.meta({
  id: "WaitId",
  description: "<waiter branch_id>:<call_id or request_id>.",
}).brand<"WaitId">();
export type WaitId = z.infer<typeof WaitId>;

import { z } from "zod";
import type { Actor } from "./common";
import { BranchId, EventId, ThreadId } from "./ids";
import { Int, PosInt, Sha256, TimeMs } from "./primitives";
import type { EnumOf, Lit, Strict } from "./zod-types";

const WRITER_IMPLS = ["threads-ts", "threads-py"] as const;

/** First line of every branch. Not an event: no seq, no prev_hash. */
export const Header: Strict<{
  format: Lit<"threads.log">;
  format_version: Lit<1>;
  thread_id: typeof ThreadId;
  branch_id: typeof BranchId;
  created_at: typeof TimeMs;
  writer: Strict<{ impl: EnumOf<typeof WRITER_IMPLS>; version: z.ZodString }>;
}> = z
  .strictObject({
    format: z.literal("threads.log"),
    format_version: z.literal(1),
    thread_id: ThreadId,
    branch_id: BranchId,
    created_at: TimeMs,
    writer: z.strictObject({
      impl: z.enum(WRITER_IMPLS),
      version: z.string().regex(/^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$/),
    }),
  })
  .meta({ id: "Header" });
export type Header = z.infer<typeof Header>;

/** Committed head checkpoint: the last line of every export. */
export const Head: Strict<{
  format: Lit<"threads.head">;
  format_version: Lit<1>;
  branch_id: typeof BranchId;
  seq: typeof Int;
  hash: typeof Sha256;
}> = z
  .strictObject({
    format: z.literal("threads.head"),
    format_version: z.literal(1),
    branch_id: BranchId,
    seq: Int,
    hash: Sha256,
  })
  .meta({ id: "Head" });
export type Head = z.infer<typeof Head>;

/** Envelope fields every event carries besides its tag, actor and data. */
export type EnvelopeShape = {
  seq: typeof PosInt;
  event_id: typeof EventId;
  thread_id: typeof ThreadId;
  branch_id: typeof BranchId;
  epoch: typeof PosInt;
  time: typeof TimeMs;
  prev_hash: typeof Sha256;
};
export const envelopeShape: EnvelopeShape = {
  seq: PosInt,
  event_id: EventId,
  thread_id: ThreadId,
  branch_id: BranchId,
  epoch: PosInt,
  time: TimeMs,
  prev_hash: Sha256,
};

/** One known `(type, type_version)` with its pinned `critical` value. */
export type EventSchema<
  T extends string,
  D extends z.core.SomeType,
  C extends boolean,
  A extends z.core.SomeType = typeof Actor,
> = Strict<
  EnvelopeShape & {
    type: Lit<T>;
    type_version: Lit<1>;
    critical: Lit<C>;
    actor: A;
    data: D;
  }
>;

export function event<
  T extends string,
  D extends z.core.SomeType,
  C extends boolean,
  A extends z.core.SomeType,
>(type: T, critical: C, actor: A, data: D): EventSchema<T, D, C, A> {
  return z.strictObject({
    ...envelopeShape,
    type: z.literal(type),
    type_version: z.literal(1),
    critical: z.literal(critical),
    actor,
    data,
  });
}

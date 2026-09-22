import { z } from "zod";
import { Actor } from "./common";
import { BranchId, EventId, ThreadId } from "./ids";
import { Int, Name, PosInt, Sha256, TimeMs } from "./primitives";
import { type Rule, withRule } from "./rules";
import type { EnumOf, Lit, Strict } from "./zod-types";

const WRITER_IMPLS = ["threads-ts", "threads-py"] as const;

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
    writer: z
      .strictObject({
        impl: z.enum(WRITER_IMPLS),
        version: z.string().regex(/^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$/),
      })
      .describe(
        "The only implementation and major version allowed to append to this branch.",
      ),
  })
  .meta({
    id: "Header",
    description:
      "First line of every branch (root or child). Not an event: no seq, no prev_hash. Readers admit format and format_version before this schema: an integer format_version above 1 is unsupported_format, any other bad version is invalid_line (README wire rule 8). The branch's first event has prev_hash = sha256 of this line. A child branch has its own header; its parent link is the fork event that follows it.",
  });
export type Header = z.infer<typeof Header>;

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
  .meta({
    id: "Head",
    description:
      "Admission as for Header (README wire rule 8). Committed head checkpoint. Stored per branch in SQLite (updated in the append transaction) and written as the last line of every export. hash is sha256 of the branch's last line (the header when seq is 0). Detects tail edits and suffix removal against the checkpoint; it is not a signature.",
  });
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
const envelopeShape: EnvelopeShape = {
  seq: PosInt,
  event_id: EventId.describe(
    "Unique along the resolved chain (every ancestor segment plus this branch), checked on append and import (semantic rule 28).",
  ),
  thread_id: ThreadId,
  branch_id: BranchId.describe(
    "The branch that wrote this event. Always the branch of the segment it is stored in.",
  ),
  epoch: PosInt.describe(
    "Writer fencing epoch from the branch lease. Non-decreasing along the resolved chain.",
  ),
  time: TimeMs,
  prev_hash: Sha256,
};

/** Any event line, known or not. Each known event narrows type, type_version, critical and data. */
export const Envelope: Strict<
  EnvelopeShape & {
    type: typeof Name;
    type_version: typeof PosInt;
    actor: typeof Actor;
    critical: z.ZodBoolean;
    data: z.ZodObject<Record<never, never>, z.core.$loose>;
  }
> = z
  .strictObject({
    seq: envelopeShape.seq,
    event_id: envelopeShape.event_id,
    thread_id: envelopeShape.thread_id,
    branch_id: envelopeShape.branch_id,
    epoch: envelopeShape.epoch,
    type: Name,
    type_version: PosInt,
    time: envelopeShape.time,
    actor: Actor,
    prev_hash: envelopeShape.prev_hash,
    critical: z
      .boolean()
      .describe(
        "true: a reader that does not know (type, type_version) must refuse the log. false: it may skip the event.",
      ),
    data: z.looseObject({}),
  })
  .meta({
    id: "Envelope",
    description:
      "Fields common to every event. seq is contiguous along the branch's resolved chain. prev_hash is sha256 of the previous line's exact bytes (without newline) within the same branch segment; the first event of a segment chains to its header.",
  });

/** One known `(type, type_version)` as parsed: the envelope with its narrowed fields. */
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

/**
 * A known event in both of its forms. `schema` parses a whole line. `fragment` is the `ev_<type>`
 * def the export lists under `Event`: only the narrowed fields, since `Event` already requires
 * the Envelope. Both come from one definition, so they can't disagree.
 */
export type EventDef<
  T extends string,
  D extends z.core.SomeType,
  C extends boolean,
  A extends z.core.SomeType = typeof Actor,
> = {
  readonly schema: EventSchema<T, D, C, A>;
  readonly fragment: z.ZodType;
};

type EventSpec<
  T extends string,
  C extends boolean,
  D extends z.core.SomeType,
> = {
  readonly type: T;
  readonly critical: C;
  readonly description?: string;
  readonly data: D;
  /** A rule over the whole event, for ties between actor and data. */
  readonly rule?: Rule;
};

function define<
  T extends string,
  C extends boolean,
  D extends z.core.SomeType,
  A extends z.core.SomeType,
>(
  spec: EventSpec<T, C, D>,
  actor: A,
  fragmentShape: z.core.$ZodLooseShape,
): EventDef<T, D, C, A> {
  const whole = z.strictObject({
    ...envelopeShape,
    type: z.literal(spec.type),
    type_version: z.literal(1),
    critical: z.literal(spec.critical),
    actor,
    data: spec.data,
  });
  const id = `ev_${spec.type}`;
  const meta =
    spec.description === undefined
      ? { id }
      : { id, description: spec.description };
  const fragment = z.object({
    type: whole.shape.type,
    type_version: whole.shape.type_version,
    critical: whole.shape.critical,
    ...fragmentShape,
  });
  return spec.rule === undefined
    ? { schema: whole, fragment: fragment.meta(meta) }
    : {
        schema: withRule(whole, spec.rule),
        fragment: withRule(fragment, spec.rule, meta),
      };
}

/** A known event whose actor is any Actor. */
export function event<
  T extends string,
  C extends boolean,
  D extends z.core.SomeType,
>(spec: EventSpec<T, C, D>): EventDef<T, D, C> {
  return define(spec, Actor, { data: spec.data });
}

/** A known event that states its actor schema. */
export function eventWithActor<
  T extends string,
  C extends boolean,
  D extends z.core.SomeType,
  A extends z.core.SomeType,
>(spec: EventSpec<T, C, D> & { readonly actor: A }): EventDef<T, D, C, A> {
  return define(spec, spec.actor, { actor: spec.actor, data: spec.data });
}

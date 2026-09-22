import { z } from "zod";
import { Envelope, Head, Header } from "./envelope";
import { EVENT_FRAGMENTS, EVENT_TYPES } from "./events";
import { type Ruled, withRule } from "./rules";
import type { EnumOf, Lit } from "./zod-types";

/** A `(type, type_version)` this schema version knows. */
export const KnownTag: z.ZodObject<{
  type: EnumOf<typeof EVENT_TYPES>;
  type_version: Lit<1>;
}> = z
  .object({ type: z.enum(EVENT_TYPES), type_version: z.literal(1) })
  .meta({ id: "KnownTag" });

/** An envelope whose `(type, type_version)` this schema version does not know. */
const UNKNOWN_EVENT_RULE: {
  readonly allOf: readonly [
    { readonly not: { readonly $ref: typeof KnownTag } },
  ];
} = { allOf: [{ not: { $ref: KnownTag } }] };
export const UnknownEvent: Ruled<typeof Envelope, typeof UNKNOWN_EVENT_RULE> =
  withRule(Envelope, UNKNOWN_EVENT_RULE, {
    id: "UnknownEvent",
    description:
      "An envelope whose (type, type_version) this schema version does not know.",
  });
export type UnknownEvent = z.infer<typeof UnknownEvent>;

// The export's view of a known event: the Envelope plus exactly one `ev_<type>` def. It accepts
// what KnownEvent parses; the parser uses KnownEvent, which is keyed on `type` and fully typed.
const Event: z.ZodIntersection<
  typeof Envelope,
  z.ZodXor<readonly z.ZodType[]>
> = z.intersection(Envelope, z.xor(EVENT_FRAGMENTS)).meta({ id: "Event" });

/** One canonical line of a branch: the root of the exported schema. */
export const LogLine: z.ZodXor<
  readonly [typeof Header, typeof Head, typeof Event, typeof UnknownEvent]
> = z.xor([Header, Head, Event, UnknownEvent]).meta({
  id: "LogLine",
  title: "threads log line (format 1)",
  description:
    "One canonical line of a threads branch: a Header (a branch's first line), an event, or the Head checkpoint (the last line of an export). SQLite stores each line's exact bytes; a JSONL export is the same bytes, one per line. A line that matches UnknownEvent is schema-valid but only admissible when critical is false (see README).",
});

import { z } from "zod";
import { Actor } from "./common";
import { type EnvelopeShape, envelopeShape, Head, Header } from "./envelope";
import { EVENT_TYPES, KnownEvent } from "./events";
import { JsonObject, PosInt } from "./primitives";
import type { EnumOf, Strict } from "./zod-types";

type UnknownShape<
  T extends z.core.SomeType,
  V extends z.core.SomeType,
> = Strict<
  EnvelopeShape & {
    type: T;
    type_version: V;
    critical: z.ZodBoolean;
    actor: typeof Actor;
    data: typeof JsonObject;
  }
>;

// "Not a known (type, type_version)" is spelled positively so it survives z.toJSONSchema:
// either the name is unknown, or the name is known at a version this schema does not have.
const unknownName = new RegExp(
  `^(?!(?:${EVENT_TYPES.join("|")})$)[a-z][a-z0-9_]{0,63}$`,
);

/** An envelope whose `(type, type_version)` this schema version does not know. */
export const UnknownEvent: z.ZodUnion<
  readonly [
    UnknownShape<z.ZodString, typeof PosInt>,
    UnknownShape<EnumOf<typeof EVENT_TYPES>, z.ZodInt>,
  ]
> = z
  .union([
    z.strictObject({
      ...envelopeShape,
      type: z.string().regex(unknownName),
      type_version: PosInt,
      critical: z.boolean(),
      actor: Actor,
      data: JsonObject,
    }),
    z.strictObject({
      ...envelopeShape,
      type: z.enum(EVENT_TYPES),
      type_version: z.int().min(2).max(Number.MAX_SAFE_INTEGER),
      critical: z.boolean(),
      actor: Actor,
      data: JsonObject,
    }),
  ])
  .meta({ id: "UnknownEvent" });
export type UnknownEvent = z.infer<typeof UnknownEvent>;

/** One canonical line of a branch: its header, an event, or the head checkpoint of an export. */
export const LogLine: z.ZodUnion<
  readonly [typeof Header, typeof Head, typeof KnownEvent, typeof UnknownEvent]
> = z.union([Header, Head, KnownEvent, UnknownEvent]).meta({
  title: "threads log line (format 1)",
  description:
    "A Header, the Head checkpoint, a known event, or an unknown event. An unknown event is admissible only when critical is false.",
});
export type LogLine = z.infer<typeof LogLine>;

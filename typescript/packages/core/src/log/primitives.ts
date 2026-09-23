import { z } from "zod";
import { type Ruled, withRule } from "./rules";

// Integers are 0 … 2^53−1 on the wire (spec/schema/README.md, wire rule 4).
export const Int: z.ZodInt = z.int().min(0).max(Number.MAX_SAFE_INTEGER).meta({
  id: "Int",
  description: "JSON integer in the cross-language safe range.",
});
const POS_INT_RULE = { allOf: [{ minimum: 1 }] } as const;
export const PosInt: Ruled<z.ZodInt, typeof POS_INT_RULE> = withRule(
  Int,
  POS_INT_RULE,
  { id: "PosInt" },
);
export const TimeMs: z.ZodInt = Int.meta({
  id: "TimeMs",
  description:
    "Unix epoch milliseconds. Informational; never used for ordering.",
});

export const Sha256: z.ZodString = z
  .string()
  .regex(/^[0-9a-f]{64}$/)
  .meta({ id: "Sha256" });
export const Uuid: z.ZodString = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
  .meta({
    id: "Uuid",
    description:
      "Lowercase UUID string. Writers MUST generate UUIDv7; readers accept any lowercase UUID, so the version is not an admission rule.",
  });
export const Name: z.ZodString = z
  .string()
  .regex(/^[a-z][a-z0-9_]{0,63}$/)
  .meta({ id: "Name" });
export const NonEmpty: z.ZodString = z.string().min(1).meta({ id: "NonEmpty" });

type JsonObjectSchema = z.ZodObject<
  Record<never, never>,
  z.core.$catchall<typeof JsonValue>
>;

/** Any JSON value. Recursive under its own name when exported. */
export const JsonValue: z.ZodType<z.core.util.JSONType> = z
  .lazy(() =>
    z.xor([
      z.null(),
      z.boolean(),
      z.number(),
      z.string(),
      z.array(JsonValue),
      jsonObject(),
    ]),
  )
  .meta({
    id: "JsonValue",
    description:
      "Any validated JSON value (tool input, model params, MCP payloads). Numbers follow.",
  });

function jsonObject(): JsonObjectSchema {
  return z.object({}).catchall(JsonValue);
}

// A separate instance from JsonValue's object branch, which the schema spells inline.
export const JsonObject: JsonObjectSchema = jsonObject().meta({
  id: "JsonObject",
});
export type JsonObject = z.infer<typeof JsonObject>;

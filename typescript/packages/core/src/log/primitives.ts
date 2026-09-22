import { z } from "zod";

// Integers are 0 … 2^53−1 on the wire (spec/schema/README.md, wire rule 4).
export const Int: z.ZodInt = z
  .int()
  .min(0)
  .max(Number.MAX_SAFE_INTEGER)
  .meta({ id: "Int" });
export const PosInt: z.ZodInt = z
  .int()
  .min(1)
  .max(Number.MAX_SAFE_INTEGER)
  .meta({ id: "PosInt" });
/** Unix epoch milliseconds. Informational; never used for ordering. */
export const TimeMs: z.ZodInt = z
  .int()
  .min(0)
  .max(Number.MAX_SAFE_INTEGER)
  .meta({ id: "TimeMs" });

export const Sha256: z.ZodString = z
  .string()
  .regex(/^[0-9a-f]{64}$/)
  .meta({ id: "Sha256" });
export const UUID_PATTERN: RegExp =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const Name: z.ZodString = z
  .string()
  .regex(/^[a-z][a-z0-9_]{0,63}$/)
  .meta({ id: "Name" });
export const NonEmpty: z.ZodString = z.string().min(1).meta({ id: "NonEmpty" });

/** Any JSON value (tool input, model params, MCP payloads). Recursive under its own name when exported. */
export const JsonValue: z.ZodType<z.core.util.JSONType> = z
  .lazy(() =>
    z.union([
      z.null(),
      z.boolean(),
      z.number(),
      z.string(),
      z.array(JsonValue),
      JsonObject,
    ]),
  )
  .meta({ id: "JsonValue" });
export const JsonObject: z.ZodRecord<z.ZodString, typeof JsonValue> = z
  .record(z.string(), JsonValue)
  .meta({ id: "JsonObject" });

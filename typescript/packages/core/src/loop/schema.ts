import { z } from "zod";
import type { JsonObject } from "../log";

/** Errors of `value` against a pinned JSON Schema, or undefined when it conforms. */
export function schemaErrors(
  schema: z.infer<typeof JsonObject>,
  value: unknown,
): string | undefined {
  let parsed: ReturnType<ReturnType<typeof z.fromJSONSchema>["safeParse"]>;
  try {
    parsed = z.fromJSONSchema(schema).safeParse(value);
  } catch {
    return "the pinned schema can't be compiled";
  }
  return parsed.success ? undefined : z.prettifyError(parsed.error);
}

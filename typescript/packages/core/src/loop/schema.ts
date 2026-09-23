import { z } from "zod";

/**
 * Errors of `value` against the tool's or agent's own Zod schema, or undefined when it parses.
 * Pinned JSON Schemas are never evaluated here: they are what the model sees.
 */
export function parseErrors(
  schema: z.ZodType,
  value: unknown,
): string | undefined {
  const parsed = schema.safeParse(value);
  return parsed.success ? undefined : z.prettifyError(parsed.error);
}

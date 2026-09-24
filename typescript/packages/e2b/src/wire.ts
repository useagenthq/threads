import { z } from "zod";

// E2B's replies as threads reads them, pinned with Python's by
// spec/conformance/vectors/e2b-wire/cases.json. As Python's `Wire`: fields threads doesn't know are
// ignored (providers add fields over time), known ones are strict (no coercion). Each schema
// lives beside the one module that reads it; this module holds what they share.

/** The sandbox error codes an E2B failure maps to (Python: `e2b/envd.py` `_checked`). */
export type Code =
  | "not_found"
  | "invalid_path"
  | "permission_denied"
  | "is_directory"
  | "too_large"
  | "unavailable";

/** An E2B answer that establishes nothing, typed by what it means. */
export class E2bError extends Error {
  override readonly name = "E2bError";
  readonly code: Code;

  constructor(code: Code, message: string) {
    super(message);
    this.code = code;
  }
}

/** A sandbox, as a create, describe or list answers it: how to reach its envd. */
export const Described: z.ZodObject<{
  sandboxID: z.ZodString;
  envdVersion: z.ZodString;
  envdAccessToken: z.ZodOptional<z.ZodNullable<z.ZodString>>;
  domain: z.ZodOptional<z.ZodNullable<z.ZodString>>;
}> = z.object({
  sandboxID: z.string().min(1),
  envdVersion: z.string(),
  envdAccessToken: z.string().nullish(),
  domain: z.string().nullish(),
});
export type Described = z.infer<typeof Described>;

/** `text` as JSON of `schema`, or the unavailable E2bError that says what was malformed. */
export function parsed<T>(schema: z.ZodType<T>, text: string, what: string): T {
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch {
    throw new E2bError("unavailable", `E2B sent ${what} that isn't JSON`);
  }
  const result = schema.safeParse(json);
  if (!result.success)
    throw new E2bError(
      "unavailable",
      `E2B sent a malformed ${what}: ${result.error.message}`,
    );
  return result.data;
}

/** The first 500 characters of a reply, for an error message. */
export async function excerpt(res: Response): Promise<string> {
  return (await res.text()).slice(0, 500);
}

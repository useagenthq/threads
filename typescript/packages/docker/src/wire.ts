import type { z } from "zod";

// What a Docker Engine API reply establishes, and the failure it becomes when it establishes
// nothing. Every reply body is a parse boundary: each module declares the schema it reads,
// known fields strict and unknown ones ignored, and a malformed body is an unavailable
// DockerError, never a cast. Python's `Wire` subclasses mirror these.

/** A Docker answer that establishes nothing, typed by what it means to the kit. */
export class DockerError extends Error {
  override readonly name = "DockerError";
  readonly code: "not_found" | "is_directory" | "unavailable";

  constructor(
    code: "not_found" | "is_directory" | "unavailable",
    message: string,
  ) {
    super(message);
    this.code = code;
  }
}

export function unavailable(message: string): DockerError {
  return new DockerError("unavailable", message);
}

/** `body` as JSON of `schema`, or the unavailable DockerError that says what was malformed. */
export function parsed<T>(
  schema: z.ZodType<T>,
  body: Uint8Array | string,
  what: string,
): T {
  const text = typeof body === "string" ? body : new TextDecoder().decode(body);
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch {
    throw unavailable(`Docker sent ${what} that isn't JSON`);
  }
  const result = schema.safeParse(json);
  if (!result.success)
    throw unavailable(
      `Docker sent a malformed ${what}: ${result.error.message}`,
    );
  return result.data;
}

/** The first 500 characters of a reply, for an error message. */
export async function excerpt(res: Response): Promise<string> {
  return (await res.text()).slice(0, 500);
}

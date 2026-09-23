import { ConfigError } from "@threads/core/adapter";
import { Ajv, type ValidateFunction } from "ajv";
import { Ajv2020 } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { z } from "zod";

// A server's tool input schema is an external JSON Schema discovered at setup (item
// 9). Core has no general JSON Schema evaluator, so the check lives here: Ajv, compiled once
// per tool, wrapped as the Zod schema the loop parses arguments with before any effect.

/** Draft 2020-12 unless the schema names an older draft. Formats are checked, not ignored. */
function ajvFor(schema: Record<string, unknown>): Ajv {
  const declared = String(schema["$schema"] ?? "");
  const options = { strict: false, allErrors: true } as const;
  const ajv = /draft-0[4-7]/.test(declared)
    ? new Ajv(options)
    : new Ajv2020(options);
  addFormats(ajv);
  return ajv;
}

/** The arguments parser for one tool; an uncompilable schema is a setup error naming it. */
export function argumentsSchema(
  tool: string,
  schema: Record<string, unknown>,
): z.ZodType {
  let validate: ValidateFunction;
  try {
    validate = ajvFor(schema).compile(schema);
  } catch (error) {
    throw new ConfigError(
      "invalid_config",
      `${tool}: the server's input schema doesn't compile: ${String(error)}`,
    );
  }
  return z.unknown().superRefine((value, ctx) => {
    if (validate(value)) return;
    for (const e of validate.errors ?? [])
      ctx.addIssue({
        code: "custom",
        message: `${e.instancePath || "/"} ${e.message ?? "is invalid"}`,
      });
  });
}

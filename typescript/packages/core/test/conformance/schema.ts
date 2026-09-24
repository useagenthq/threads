import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";

// Test-only: spec/conformance/case.schema.json compiled with z.fromJSONSchema, so tests can check
// a saved case against the real schema. Core carries no JSON Schema evaluator (api.json saveCase).
// The events and eval schemas' $defs are merged in under `ev__` and `eval__` prefixes, since
// their refs are external.

const SPEC = join(import.meta.dir, "../../../../../spec");
const EVENTS = "urn:threads:schema:events:v1#/$defs/";
const EVAL = "urn:threads:schema:eval:v1#/$defs/";

function read(path: string): string {
  return readFileSync(join(SPEC, path), "utf8");
}

type Json = z.core.util.JSONType;
const JsonObject = z.record(z.string(), z.json());

function merged(): Record<string, Json> {
  const cases = JsonObject.parse(
    JSON.parse(
      read("conformance/case.schema.json")
        .replaceAll(EVENTS, "#/$defs/ev__")
        .replaceAll(EVAL, "#/$defs/eval__"),
    ),
  );
  const events = JsonObject.parse(
    JSON.parse(
      read("schema/events.v1.schema.json").replaceAll(
        '"#/$defs/',
        '"#/$defs/ev__',
      ),
    ),
  );
  const evals = JsonObject.parse(
    JSON.parse(
      read("schema/eval.v1.schema.json")
        .replaceAll('"#/$defs/', '"#/$defs/eval__')
        .replaceAll(EVENTS, "#/$defs/ev__"),
    ),
  );
  const defs: Record<string, Json> = {};
  for (const [key, value] of Object.entries(JsonObject.parse(cases["$defs"])))
    defs[key] = unconditional(value);
  for (const [key, value] of Object.entries(JsonObject.parse(events["$defs"])))
    defs[`ev__${key}`] = unconditional(value);
  for (const [key, value] of Object.entries(JsonObject.parse(evals["$defs"])))
    defs[`eval__${key}`] = unconditional(value);
  const { $id: _id, ...rest } = cases;
  return { ...rest, $defs: defs };
}

/**
 * z.fromJSONSchema can't compile if/then/else: the events schema's (the critical rule, fork's
 * snapshot/repair split) and Expected's (error required when outcome is error). Dropping them
 * checks every field but those couplings; a saved case's logs are still verified in full when a
 * runner imports them.
 */
function unconditional(value: Json): Json {
  if (Array.isArray(value)) return value.map(unconditional);
  if (typeof value !== "object" || value === null) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([k]) => k !== "if" && k !== "then" && k !== "else")
      .map(([k, v]) => [k, unconditional(v)]),
  );
}

const ROOT = merged();

/** A validator for one `$defs` entry of case.schema.json (Case, Expected, ModelScript, ...). */
export function caseSchema(def: string): z.ZodType {
  return z.fromJSONSchema({ ...ROOT, $ref: `#/$defs/${def}` });
}

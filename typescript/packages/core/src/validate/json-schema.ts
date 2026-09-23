import { FORMATS, multipleOf } from "./formats";

// The JSON Schema evaluator semantic rule 20 checks an output value with: the keyword set that
// spec/schema/README.md ("Output schemas") defines, the same as the Python reader
// (threads/_json_schema.py). Never a library's reading of JSON Schema, so both languages agree.
// A keyword it can't check throws TypeError rather than passing; `unchecked` names one anywhere
// in a schema, so an output schema that uses it is refused at setup instead of failing a run.

type Json = Readonly<Record<string, unknown>>;
type Check = (argument: unknown, value: unknown, root: Json) => boolean;

const ANNOTATIONS: ReadonlySet<string> = new Set([
  "description",
  "title",
  "default",
  "examples",
  "$defs",
  "$schema",
  "$comment",
]);
const DEFS = "#/$defs/";

/** Whether `value` satisfies `schema`. A `$ref` is `#` or names one of its `$defs`. */
export function holds(schema: unknown, value: unknown): boolean {
  return holdsIn(schema, value, isObject(schema) ? schema : {});
}

/**
 * Semantic rule 20: whether an output value satisfies the pinned output schema. A keyword this
 * reader can't check fails closed rather than accepting unchecked output.
 */
export function conforms(schema: unknown, value: unknown): boolean {
  try {
    return holds(schema, value);
  } catch (error) {
    if (error instanceof TypeError) return false;
    throw error;
  }
}

/** The first keyword `holds` can't check anywhere in `schema`, else undefined. */
export function unchecked(schema: unknown): string | undefined {
  try {
    walk(schema);
    return undefined;
  } catch (error) {
    if (error instanceof TypeError) return error.message;
    throw error;
  }
}

function holdsIn(schema: unknown, value: unknown, root: Json): boolean {
  if (typeof schema === "boolean") return schema;
  if (!isObject(schema)) throw new TypeError(`not a schema: ${String(schema)}`);
  if ("if" in schema) {
    const branch = holdsIn(schema["if"], value, root)
      ? schema["then"]
      : schema["else"];
    if (branch !== undefined && !holdsIn(branch, value, root)) return false;
  }
  if ("additionalProperties" in schema && !additional(schema, value, root))
    return false;
  return Object.entries(schema).every(([keyword, argument]) =>
    skipped(keyword) ? true : checkOf(keyword)(argument, value, root),
  );
}

function skipped(keyword: string): boolean {
  return (
    ANNOTATIONS.has(keyword) ||
    ["if", "then", "else", "additionalProperties"].includes(keyword)
  );
}

function checkOf(keyword: string): Check {
  const check = CHECKS.get(keyword);
  if (check === undefined)
    throw new TypeError(`unsupported schema keyword '${keyword}'`);
  return check;
}

const SCHEMA_ARG: ReadonlySet<string> = new Set([
  "not",
  "if",
  "then",
  "else",
  "items",
  "additionalProperties",
]);
const LIST_ARG: ReadonlySet<string> = new Set(["allOf", "anyOf", "oneOf"]);
const MAP_ARG: ReadonlySet<string> = new Set(["properties", "$defs"]);

function walk(schema: unknown): void {
  if (typeof schema === "boolean") return;
  if (!isObject(schema)) throw new TypeError(`not a schema: ${String(schema)}`);
  for (const [keyword, argument] of Object.entries(schema)) {
    admit(keyword, argument);
    for (const sub of subschemas(keyword, argument)) walk(sub);
  }
}

function admit(keyword: string, argument: unknown): void {
  if (keyword === "pattern") compile(argument);
  else if (keyword === "$ref") refName(argument);
  else if (keyword === "format" && !FORMATS.has(String(argument)))
    throw new TypeError(`unsupported format '${String(argument)}'`);
  else if (!CHECKS.has(keyword) && !skipped(keyword))
    throw new TypeError(`unsupported schema keyword '${keyword}'`);
}

function subschemas(keyword: string, argument: unknown): readonly unknown[] {
  if (SCHEMA_ARG.has(keyword)) return [argument];
  if (LIST_ARG.has(keyword)) return schemas(argument);
  if (MAP_ARG.has(keyword)) return Object.values(map(argument));
  return [];
}

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function schemas(argument: unknown): readonly unknown[] {
  if (!Array.isArray(argument))
    throw new TypeError("expected a list of schemas");
  return argument;
}

function map(argument: unknown): Json {
  if (!isObject(argument)) throw new TypeError("expected a map of schemas");
  return argument;
}

function jsonEqual(a: unknown, b: unknown): boolean {
  if (Array.isArray(a))
    return (
      Array.isArray(b) &&
      a.length === b.length &&
      a.every((x, i) => jsonEqual(x, b[i]))
    );
  if (isObject(a)) {
    if (!isObject(b)) return false;
    const keys = Object.keys(a);
    return (
      keys.length === Object.keys(b).length &&
      keys.every((k) => k in b && jsonEqual(a[k], b[k]))
    );
  }
  return a === b;
}

/** The `$defs` entry a reference names, or undefined for `#`, the whole schema. */
function refName(ref: unknown): string | undefined {
  if (ref === "#") return undefined;
  if (typeof ref !== "string" || !ref.startsWith(DEFS))
    throw new TypeError(`unsupported $ref '${String(ref)}'`);
  return ref.slice(DEFS.length);
}

function ref(argument: unknown, value: unknown, root: Json): boolean {
  const name = refName(argument);
  if (name === undefined) return holdsIn(root, value, root);
  const defs = map(root["$defs"] ?? {});
  if (!(name in defs))
    throw new TypeError(`$ref to a missing definition '${name}'`);
  return holdsIn(defs[name], value, root);
}

function compile(argument: unknown): RegExp {
  if (typeof argument !== "string")
    throw new TypeError("a pattern is a string");
  try {
    return new RegExp(argument);
  } catch {
    throw new TypeError(`pattern '${argument}' doesn't compile`);
  }
}

function format(argument: unknown, value: unknown): boolean {
  const check = FORMATS.get(String(argument));
  if (check === undefined)
    throw new TypeError(`unsupported format '${String(argument)}'`);
  return typeof value !== "string" || check(value);
}

function number(argument: unknown): number {
  if (typeof argument !== "number") throw new TypeError("a bound is a number");
  return argument;
}

const TYPES: ReadonlyMap<string, (value: unknown) => boolean> = new Map<
  string,
  (value: unknown) => boolean
>([
  ["object", isObject],
  ["array", Array.isArray],
  ["string", (v) => typeof v === "string"],
  ["boolean", (v) => typeof v === "boolean"],
  ["null", (v) => v === null],
  ["integer", (v) => typeof v === "number" && Number.isInteger(v)],
  ["number", (v) => typeof v === "number"],
]);

function type(argument: unknown, value: unknown): boolean {
  const names: readonly unknown[] = Array.isArray(argument)
    ? argument
    : [argument];
  return names.some((name) => {
    const is = typeof name === "string" ? TYPES.get(name) : undefined;
    if (is === undefined)
      throw new TypeError(`unknown JSON type '${String(name)}'`);
    return is(value);
  });
}

type Compare = (a: number, b: number) => boolean;

function bound(compare: Compare): Check {
  return (argument, value) => {
    const limit = number(argument);
    return typeof value !== "number" || compare(value, limit);
  };
}

/** A string counts code points, as JSON Schema does; an array its items, an object its keys. */
function sizeOf(value: unknown): number | undefined {
  if (typeof value === "string") return [...value].length;
  if (Array.isArray(value)) return value.length;
  return isObject(value) ? Object.keys(value).length : undefined;
}

function size(of: "string" | "array" | "object", compare: Compare): Check {
  return (argument, value) => {
    const limit = number(argument);
    const n = TYPES.get(of)?.(value) === true ? sizeOf(value) : undefined;
    return n === undefined || compare(n, limit);
  };
}

function additional(schema: Json, value: unknown, root: Json): boolean {
  if (!isObject(value)) return true;
  const declared = map(schema["properties"] ?? {});
  return Object.keys(value)
    .filter((key) => !(key in declared))
    .every((key) => holdsIn(schema["additionalProperties"], value[key], root));
}

const ge: Compare = (a, b) => a >= b;
const le: Compare = (a, b) => a <= b;

const CHECKS: ReadonlyMap<string, Check> = new Map<string, Check>([
  ["not", (arg, value, root) => !holdsIn(arg, value, root)],
  ["allOf", (arg, v, root) => schemas(arg).every((s) => holdsIn(s, v, root))],
  ["anyOf", (arg, v, root) => schemas(arg).some((s) => holdsIn(s, v, root))],
  [
    "oneOf",
    (arg, v, root) =>
      schemas(arg).filter((s) => holdsIn(s, v, root)).length === 1,
  ],
  ["const", (arg, value) => jsonEqual(arg, value)],
  ["enum", (arg, value) => schemas(arg).some((e) => jsonEqual(e, value))],
  [
    "required",
    (arg, value) =>
      !isObject(value) || schemas(arg).every((k) => String(k) in value),
  ],
  [
    "properties",
    (arg, value, root) =>
      !isObject(value) ||
      Object.entries(map(arg)).every(
        ([key, sub]) => !(key in value) || holdsIn(sub, value[key], root),
      ),
  ],
  ["type", type],
  [
    "items",
    (arg, value, root) =>
      !Array.isArray(value) || value.every((item) => holdsIn(arg, item, root)),
  ],
  ["$ref", ref],
  [
    "pattern",
    (arg, value) => typeof value !== "string" || compile(arg).test(value),
  ],
  ["format", format],
  [
    "multipleOf",
    (arg, value) => typeof value !== "number" || multipleOf(value, number(arg)),
  ],
  ["minimum", bound(ge)],
  ["maximum", bound(le)],
  ["exclusiveMinimum", bound((a, b) => a > b)],
  ["exclusiveMaximum", bound((a, b) => a < b)],
  ["minLength", size("string", ge)],
  ["maxLength", size("string", le)],
  ["minItems", size("array", ge)],
  ["maxItems", size("array", le)],
  ["minProperties", size("object", ge)],
]);

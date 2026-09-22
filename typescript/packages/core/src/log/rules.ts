import { z } from "zod";
import type { Narrow } from "./narrow";

// Cross-field rules (if/then/else, not, oneOf over required, ...) have no Zod form. A rule is
// written once, as JSON Schema data: the export copies it into the schema verbatim and `holds`
// enforces it at parse time, as python/src/threads/_strict_model.py does for the same data.

type Scalar = string | number | boolean | null;

/** The JSON Schema subset a rule may use. `$ref` names another schema of this module. */
export type Rule = {
  readonly $ref?: z.ZodType;
  readonly if?: Rule;
  readonly then?: Rule;
  readonly else?: Rule;
  readonly not?: Rule;
  readonly allOf?: readonly Rule[];
  readonly anyOf?: readonly Rule[];
  readonly oneOf?: readonly Rule[];
  readonly const?: Scalar;
  readonly enum?: readonly Scalar[];
  readonly required?: readonly string[];
  readonly properties?: { readonly [key: string]: Rule };
  readonly minProperties?: number;
  readonly minimum?: number;
};

type Meta = { readonly id?: string; readonly description?: string };

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function propertiesHold(rule: Rule, value: unknown): boolean {
  const { properties } = rule;
  if (properties === undefined || !isObject(value)) return true;
  return Object.entries(properties).every(
    ([key, sub]) => !(key in value) || holds(sub, value[key]),
  );
}

function objectHolds(rule: Rule, value: unknown): boolean {
  if (!isObject(value)) return true;
  const { required = [], minProperties = 0 } = rule;
  return (
    required.every((key) => key in value) &&
    Object.keys(value).length >= minProperties
  );
}

function valueHolds(rule: Rule, value: unknown): boolean {
  // Strict equality keeps JSON types apart: true is not 1.
  if ("const" in rule && rule.const !== value) return false;
  if (rule.enum !== undefined && !rule.enum.some((item) => item === value))
    return false;
  const { minimum } = rule;
  return minimum === undefined || typeof value !== "number" || value >= minimum;
}

function branchHolds(rule: Rule, value: unknown): boolean {
  if (rule.if === undefined) return true;
  const branch = holds(rule.if, value) ? rule.then : rule.else;
  return branch === undefined || holds(branch, value);
}

function combinationsHold(rule: Rule, value: unknown): boolean {
  const { allOf = [], anyOf, oneOf, not } = rule;
  return (
    allOf.every((sub) => holds(sub, value)) &&
    (anyOf === undefined || anyOf.some((sub) => holds(sub, value))) &&
    (oneOf === undefined ||
      oneOf.filter((sub) => holds(sub, value)).length === 1) &&
    (not === undefined || !holds(not, value))
  );
}

/** Whether `value` satisfies `rule`, with JSON Schema semantics for the subset above. */
export function holds(rule: Rule, value: unknown): boolean {
  return (
    (rule.$ref === undefined || rule.$ref.safeParse(value).success) &&
    valueHolds(rule, value) &&
    objectHolds(rule, value) &&
    propertiesHold(rule, value) &&
    branchHolds(rule, value) &&
    combinationsHold(rule, value)
  );
}

/** The rule as it appears in the exported schema: each `$ref` points at its schema's def. */
function toJson(
  rule: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(rule).map(([key, value]) => [key, jsonValue(value)]),
  );
}

function jsonValue(value: unknown): unknown {
  if (value instanceof z.ZodType) {
    const id = z.globalRegistry.get(value)?.id;
    if (id === undefined) throw new Error("a rule's $ref needs a schema id");
    return `#/$defs/${id}`;
  }
  if (Array.isArray(value)) return value.map(jsonValue);
  return isObject(value) ? toJson(value) : value;
}

/** A schema whose parsed type is narrowed by the rule `R` (see narrow.ts). */
export type Ruled<T extends z.ZodType, R> = T &
  z.ZodType<z.output<T> & Narrow<z.output<T>, R>, z.input<T>>;

/** `schema` narrowed by `rule`, which is enforced on parse, typed and exported as written. */
export function withRule<T extends z.ZodType, const R extends Rule>(
  schema: T,
  rule: R,
  meta: Meta = {},
): Ruled<T, R> {
  const proves = (
    value: z.output<T>,
  ): value is z.output<T> & Narrow<z.output<T>, R> => holds(rule, value);
  const narrowed = schema.refine(proves, "violates a schema rule");
  // Registered on the refined schema itself: a second clone would hide the parent's $ref.
  const registered: z.ZodType = narrowed;
  z.globalRegistry.add(registered, { ...meta, ...toJson(rule) });
  return narrowed;
}

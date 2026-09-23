import { z } from "zod";
import {
  CacheBreak,
  ContentPart,
  Cost,
  ErrorCode,
  KnownTag,
  LogLine,
  TextOrContent,
  TextOrRef,
} from "../src/log";

// Builds spec/schema/events.v1.schema.json from the Zod log schema. Anything Zod
// can't represent throws. The passes below only drop or re-spell keywords, never change meaning.

type Json = z.core.util.JSONType;
type Node = { [key: string]: Json };

// Defs that only a rule's `$ref` or the wire contract names; the walk from LogLine misses them.
const EXTRA_DEFS = [
  ErrorCode,
  KnownTag,
  TextOrRef,
  TextOrContent,
  ContentPart,
  Cost,
  CacheBreak,
];

const SUBSCHEMA_MAPS = new Set(["properties", "$defs"]);
const SUBSCHEMA_LISTS = new Set(["allOf", "anyOf", "oneOf", "prefixItems"]);
const SUBSCHEMAS = new Set([
  "not",
  "if",
  "then",
  "else",
  "items",
  "additionalProperties",
  "propertyNames",
]);

function isNode(value: Json | undefined): value is Node {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function omit(node: Node, keys: readonly string[]): Node {
  return Object.fromEntries(
    Object.entries(node).filter(([key]) => !keys.includes(key)),
  );
}

/** Rebuilds every schema object in the tree through `fn`, children first. */
function transform(node: Node, fn: (node: Node) => Node): Node {
  const sub = (value: Json): Json =>
    isNode(value) ? transform(value, fn) : value;
  const children = Object.entries(node).map(([key, value]): [string, Json] => {
    if (SUBSCHEMA_MAPS.has(key) && isNode(value)) {
      const entries = Object.entries(value).map(([k, v]) => [k, sub(v)]);
      return [key, Object.fromEntries(entries)];
    }
    if (SUBSCHEMA_LISTS.has(key) && Array.isArray(value)) {
      return [key, value.map(sub)];
    }
    return [key, SUBSCHEMAS.has(key) ? sub(value) : value];
  });
  return fn(Object.fromEntries(children));
}

/** Drops each keyword beside `$ref` that the referenced def, or its own `$ref` chain, states. */
function dropInherited(defs: Node): (node: Node) => Node {
  const target = ({ $ref }: Node): Node | undefined => {
    const def =
      typeof $ref === "string" ? defs[$ref.replace("#/$defs/", "")] : undefined;
    return isNode(def) ? def : undefined;
  };
  const same = (a: Json | undefined, b: Json | undefined): boolean =>
    JSON.stringify(a) === JSON.stringify(b);
  return (node) => {
    let out = node;
    for (let def = target(node); def; def = target(def)) {
      const stated = def;
      const redundant = Object.keys(out).filter(
        (key) => key !== "$ref" && same(out[key], stated[key]),
      );
      out = omit(out, redundant);
    }
    return out;
  };
}

/** Zod spellings that say nothing the rest of the node doesn't. */
function respell(node: Node): Node {
  const empty = ["properties", "additionalProperties"].filter((key) => {
    const value = node[key];
    return isNode(value) && Object.keys(value).length === 0;
  });
  // `const` and `enum` already fix the type.
  const typed = "const" in node || "enum" in node ? ["type"] : [];
  let out = omit(node, [...empty, ...typed]);
  const { pattern, $ref, allOf } = out;
  if (typeof pattern === "string") {
    // RegExp.prototype.source escapes "/"; both regex dialects read "\/" as "/".
    const unescaped = pattern.replace(/(?<!\\)((?:\\\\)*)\\\//g, "$1/");
    out = { ...out, pattern: unescaped };
  }
  if (typeof $ref === "string" && Array.isArray(allOf)) {
    // A `$ref` beside `allOf` is one more conjunct.
    out = { ...omit(out, ["$ref"]), allOf: [{ $ref }, ...allOf] };
  }
  return out;
}

export function specSchema(): Node {
  const exported = z.toJSONSchema(z.tuple([LogLine, ...EXTRA_DEFS]), {
    target: "draft-2020-12",
    io: "input",
    unrepresentable: "throw",
  });
  const tree: Json = JSON.parse(JSON.stringify(exported));
  const { $defs: defs } = isNode(tree) ? tree : {};
  if (!isNode(defs)) throw new Error("export has no $defs");
  const { LogLine: root, ...rest } = defs;
  if (!isNode(root)) throw new Error("export has no LogLine def");
  const schema: Node = {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "urn:threads:schema:events:v1",
    ...root,
    $defs: rest,
  };
  return transform(transform(schema, dropInherited(rest)), respell);
}

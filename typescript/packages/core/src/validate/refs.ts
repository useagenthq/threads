// `$ref` checks for semantic rule 20 (spec/schema/README.md, Output schemas): every reference
// names `#` or an existing `$defs` entry, and no chain of references comes back to where it
// started without reaching into the value (`{"$ref": "#"}` would check the same value forever).
// threads/_json_refs.py is the Python side.

const DEFS = "#/$defs/";
/** Keywords whose subschema checks the same value, not a part of it. */
const SAME_VALUE = ["not", "if", "then", "else"];
const SAME_VALUE_LISTS = ["allOf", "anyOf", "oneOf"];

type Json = Readonly<Record<string, unknown>>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Throws TypeError for a reference to nothing, or a reference cycle that consumes no input. */
export function checkRefs(schema: Json): void {
  const defs = schema["$defs"] ?? {};
  if (!isObject(defs)) throw new TypeError("$defs is not a map of schemas");
  const graph = new Map<string, ReadonlySet<string>>([
    ["#", sameValueRefs(schema)],
    ...Object.entries(defs).map(
      ([name, def]) => [`${DEFS}${name}`, sameValueRefs(def)] as const,
    ),
  ]);
  const missing = [...everyRef(schema)].filter((ref) => !graph.has(ref)).sort();
  if (missing[0] !== undefined)
    throw new TypeError(`$ref to a missing definition '${missing[0]}'`);
  const done = new Set<string>();
  for (const start of graph.keys()) noCycle(graph, start, [], done);
}

/** Every reference anywhere in the schema. */
function everyRef(node: unknown): ReadonlySet<string> {
  if (Array.isArray(node))
    return new Set(node.flatMap((n) => [...everyRef(n)]));
  if (!isObject(node)) return new Set();
  const ref = node["$ref"];
  return new Set([
    ...(typeof ref === "string" ? [ref] : []),
    ...Object.values(node).flatMap((v) => [...everyRef(v)]),
  ]);
}

/** The references `schema` follows while still checking the same value. */
function sameValueRefs(schema: unknown): ReadonlySet<string> {
  const found = new Set<string>();
  const stack: unknown[] = [schema];
  for (let node = stack.pop(); node !== undefined; node = stack.pop()) {
    if (!isObject(node)) continue;
    const ref = node["$ref"];
    if (typeof ref === "string") found.add(ref);
    stack.push(...sameValue(node));
  }
  return found;
}

/** The subschemas of `node` that check the same value it does. */
function sameValue(node: Json): readonly unknown[] {
  const one = SAME_VALUE.filter((k) => Object.hasOwn(node, k)).map(
    (k) => node[k],
  );
  const listed = SAME_VALUE_LISTS.flatMap((k) => {
    const value = node[k];
    return Object.hasOwn(node, k) && Array.isArray(value) ? value : [];
  });
  return [...one, ...listed];
}

/** Depth first from `at`; `done` holds the nodes already proven to lead to no cycle. */
function noCycle(
  graph: ReadonlyMap<string, ReadonlySet<string>>,
  at: string,
  path: readonly string[],
  done: Set<string>,
): void {
  if (done.has(at)) return;
  if (path.includes(at))
    throw new TypeError(
      `$refs loop without reaching into the value: ${[...path, at].join(" -> ")}`,
    );
  for (const target of [...(graph.get(at) ?? [])].sort())
    noCycle(graph, target, [...path, at], done);
  done.add(at);
}

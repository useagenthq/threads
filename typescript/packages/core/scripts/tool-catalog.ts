import { z } from "zod";
import { jsonSchema } from "../src/agent/tool";
import { canonicalize } from "../src/log";
import { CATALOG } from "../src/tools/catalog";

// The built-in tool catalog as spec artifacts (src/tools/catalog.ts is the one source):
// - tools.v1.schema.json: each tool's input schema as a $def, for Python's model generator;
// - tools.v1.catalog.json: [{name, description, input_schema}] sorted by name, as RFC 8785
//   bytes. These are exactly the specs line 0 pins, so both runtimes compare against them.

type Json = z.core.util.JSONType;

const pascal = (name: string): string =>
  name
    .split("_")
    .map((w) => `${w.slice(0, 1).toUpperCase()}${w.slice(1)}`)
    .join("");

export function toolSchema(): Json {
  return {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "urn:threads:schema:tools:v1",
    title: "Built-in tool inputs",
    description:
      "Generated from typescript/packages/core/src/tools/catalog.ts by bun run schema:export. Do not edit.",
    $defs: Object.fromEntries(
      CATALOG.map((e) => [
        `${pascal(e.name)}Input`,
        jsonSchema(e.name, e.input),
      ]),
    ),
  };
}

/** The canonical catalog bytes (RFC 8785), no trailing newline: what the model is shown. */
export function toolCatalog(): string {
  const listed = CATALOG.map((e) => ({
    name: e.name,
    description: e.description,
    input_schema: jsonSchema(e.name, e.input),
  })).toSorted((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const text = canonicalize(z.json().parse(listed));
  if (!text.ok) throw new Error(text.error.message);
  return text.value;
}

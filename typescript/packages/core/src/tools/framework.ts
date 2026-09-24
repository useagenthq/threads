import { jsonSchema } from "../agent/tool";
import { ToolSpec } from "../log";
import { entry } from "./catalog";
import { byCodePoint } from "./tool-search";

// The pinned spec of a framework tool: its catalog entry, read_only because it
// changes only log state. The loop runs it (loop/framework.ts), so it has no ToolImpl.

export function frameworkSpec(name: string): ToolSpec {
  const listed = entry(name);
  return ToolSpec.parse({
    name,
    description: listed.description,
    input_schema: jsonSchema(name, listed.input),
    effect_class: "read_only",
  });
}

/**
 * tool_search as pinned when anything is deferred: its catalog entry, with the deferred names,
 * sorted by code point, listed after its description. The pin, member rebind and goldens call it.
 */
export function searchToolSpec(deferredNames: readonly string[]): ToolSpec {
  const spec = frameworkSpec("tool_search");
  const names = deferredNames.toSorted(byCodePoint).join(", ");
  return {
    ...spec,
    description: `${spec.description}\n\nDeferred tools (search to load): ${names}`,
  };
}

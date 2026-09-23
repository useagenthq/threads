import { jsonSchema } from "../agent/tool";
import { ToolSpec } from "../log";
import { entry } from "./catalog";

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

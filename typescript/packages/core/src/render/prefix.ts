import type { Json, KnownEvent, ToolSpec } from "../log";
import { canonicalize } from "../log";

/** A tool as the model sees it: a deferred spec is a stub; dispatch-only fields are omitted. */
export function toolLine(spec: ToolSpec): Json {
  const { name, description } = spec;
  return spec.defer_loading === true
    ? { name, description, deferred: true }
    : { name, description, input_schema: spec.input_schema };
}

/** RFC 8785 text of a rendered line. Render lines nest no deeper than the events they come from. */
export function jcs(value: Json): string {
  const text = canonicalize(value);
  if (!text.ok)
    throw new Error(`render line not canonical: ${text.error.message}`);
  return text.value;
}

/**
 * Render v1 line 0 without its `\n`: system and tools from `thread_started`, model, params and
 * adapter from the latest `thread_started` or `settings_changed` (the request's settings epoch).
 */
export function line0(events: readonly KnownEvent[]): string {
  const started = events.find((e) => e.type === "thread_started");
  if (started?.type !== "thread_started")
    throw new Error("a verified chain starts with thread_started");
  let settings: Pick<
    typeof started.data,
    "model" | "model_params" | "adapter"
  > = started.data;
  for (const e of events)
    if (e.type === "settings_changed") settings = e.data.settings;
  return jcs({
    adapter: settings.adapter,
    model: settings.model,
    params: settings.model_params,
    system: started.data.instructions,
    tools: started.data.tools.map(toolLine),
  });
}

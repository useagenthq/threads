import type { z } from "zod";
import { sha256Hex } from "../hash";
import { type ContextPolicy, canonicalize, JsonValue, ToolSpec } from "../log";
import { ConfigError } from "./errors";
import type { Tool } from "./tool";

// Deferred tools at pin time (spec/schema/README.md, "Deferred tools and tool_search"): which
// user tools are deferred, and each one's reference form with its spec artifact, stored before
// thread_started names it.

export type DeferTools = z.infer<typeof ContextPolicy>["defer_tools"];

/**
 * A child's context: its own defer_tools, else its parent's resolved one (a subagent, team
 * member or handoff target inherits it; loaded tools are never inherited).
 */
export function inheritDefer<C extends { readonly defer_tools?: DeferTools }>(
  context: C,
  parent: DeferTools | undefined,
): C {
  return context.defer_tools !== undefined || parent === undefined
    ? context
    : { ...context, defer_tools: parent };
}

const encoder = new TextEncoder();

/**
 * The names of the user tools (app, extension and MCP) `mode` defers: auto honours each tool's
 * `defer`, never defers none, always defers every one that doesn't end the turn.
 */
export function deferredNames(
  user: readonly Tool<unknown, unknown, never>[],
  mode: DeferTools,
): ReadonlySet<string> {
  const deferred = user.filter((t) =>
    mode === "always"
      ? t.spec().ends_turn !== true
      : mode === "auto" && t.defer === true,
  );
  const names = new Set(deferred.map((t) => t.name));
  if (names.size > 0 && user.some((t) => t.name === "tool_search"))
    throw new ConfigError(
      "invalid_config",
      "tool tool_search: the name is taken by the built-in that loads deferred tools; rename it",
    );
  return names;
}

/** A deferred tool's pinned stub and the bytes of its spec artifact. */
export function referenceForm(spec: ToolSpec): {
  readonly stub: ToolSpec;
  readonly bytes: Uint8Array;
} {
  const json = JsonValue.safeParse(spec);
  const text = json.success ? canonicalize(json.data) : undefined;
  if (text?.ok !== true)
    throw new ConfigError(
      "invalid_config",
      `tool ${spec.name}: spec is not JSON`,
    );
  const bytes = encoder.encode(text.value);
  const { name, description, effect_class, dedup_window_ms, ends_turn } = spec;
  return {
    bytes,
    stub: ToolSpec.parse({
      name,
      description,
      effect_class,
      ...(dedup_window_ms === undefined ? {} : { dedup_window_ms }),
      ...(ends_turn === undefined ? {} : { ends_turn }),
      defer_loading: true,
      spec_ref: {
        sha256: sha256Hex(bytes),
        bytes: bytes.length,
        media_type: "application/json",
      },
    }),
  };
}

/** Puts a pin's spec artifacts; each is durable before any thread_started names it. */
export function storeSpecs(
  artifacts: { readonly put: (bytes: Uint8Array) => string },
  specs: readonly Uint8Array[],
): void {
  for (const bytes of specs) artifacts.put(bytes);
}

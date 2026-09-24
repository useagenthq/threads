import type { ToolSpec } from "../log";
import { TEAM_TOOLS as TEAM_MEMBER_TOOLS } from "../team/constants";
import { isLoopTool } from "../tools/loop-tools";
import type { CaseDir } from "./case-dir";

// Whether a case reruns offline from its directory alone (spec lane 22, A.2 and A.4): the
// reason saveCase recorded, or what an older case's files can't answer. The runner skips such
// a case with `offline_not_runnable:<reason>`.

/** Tools that start or hand to another thread: a child whose model calls the case lacks. */
export const CHILD_TOOLS: ReadonlySet<string> = new Set([
  "spawn_agent",
  "handoff",
  "start",
]);
/** The team model tools: they act on member threads the rerun doesn't have. */
export const TEAM_TOOLS: ReadonlySet<string> = new Set([
  ...TEAM_MEMBER_TOOLS,
  "send_message",
  "team_task_claim",
  "team_task_create",
  "team_task_update",
]);

/** The tool names the recorded model replies call, in order. */
function toolUses(model: unknown): readonly string[] {
  if (typeof model !== "object" || model === null) return [];
  const responses: unknown = Reflect.get(model, "responses");
  if (!Array.isArray(responses)) return [];
  return responses.flatMap((r: unknown) => {
    const content: unknown =
      typeof r === "object" && r !== null
        ? Reflect.get(r, "content")
        : undefined;
    return Array.isArray(content)
      ? content.flatMap((p: unknown) => {
          const kind: unknown =
            typeof p === "object" && p !== null
              ? Reflect.get(p, "type")
              : undefined;
          const name: unknown =
            typeof p === "object" && p !== null
              ? Reflect.get(p, "name")
              : undefined;
          return kind === "tool_use" && typeof name === "string" ? [name] : [];
        })
      : [];
  });
}

/**
 * A case with no sandbox.json (a Python save_case before this lane): runnable only when the
 * turn made no read-only call that needs a recorded result. A framework tool never does; a
 * name the tool set in effect doesn't know does.
 */
function withoutResults(
  uses: readonly string[],
  tools: readonly ToolSpec[],
): string | undefined {
  const needed = uses.filter((name) => {
    if (isLoopTool(name)) return false;
    const spec = tools.find((t) => t.name === name);
    return spec === undefined || spec.effect_class === "read_only";
  });
  return needed.length === 0 ? undefined : "resave_case";
}

/** A v1 sandbox.json keeps one preview per tool name: usable only if each name ran once. */
function v1Usable(c: CaseDir, uses: readonly string[]): string | undefined {
  const sandbox = c.sandbox;
  if (sandbox === undefined || "results" in sandbox) return undefined;
  const names = Object.keys(sandbox.tools ?? {});
  const once = names.every((n) => uses.filter((u) => u === n).length === 1);
  return once ? undefined : "resave_case";
}

/** The reason `c` can't rerun offline, or undefined. `tools`: the set in effect at the turn. */
export function offlineBlock(
  c: CaseDir,
  tools: readonly ToolSpec[],
): string | undefined {
  if (c.meta.offline !== undefined) return c.meta.offline.reason;
  const uses = toolUses(c.model);
  if (uses.some((u) => CHILD_TOOLS.has(u))) return "child_threads";
  if (uses.some((u) => TEAM_TOOLS.has(u))) return "team_calls";
  if (c.sandbox === undefined) return withoutResults(uses, tools);
  return v1Usable(c, uses);
}

import type { z } from "zod";
import { assertNever } from "../assert-never";
import type { EffectClass, PermissionMode, PermissionsPolicy } from "../log";
import {
  allMatch,
  anyMatch,
  globMatches,
  isFileTool,
  shellOf,
  workspacePath,
} from "./match";

// the evaluation order. Thread rules come later, with
// permission_rule_added.

type Policy = z.infer<typeof PermissionsPolicy>;
type Mode = z.infer<typeof PermissionMode>;

export type Category = "read_only" | "edit" | "other";

export type PermissionCall = {
  readonly tool: string;
  readonly category: Category;
  readonly input: Readonly<Record<string, unknown>>;
  readonly mode: Mode;
};

export type Decision = {
  readonly decision: "allow" | "deny" | "ask";
  readonly source:
    | "policy"
    | "mode"
    | "protected_path"
    | "self_config_guard"
    | "thread_rule";
  readonly rule?: string;
};

export const DEFAULT_PERMISSIONS: Policy = {
  mode: "default",
  allow: [],
  ask: [],
  deny: [],
  protected_paths: [
    ".git/**",
    ".threads/**",
    ".claude/**",
    ".mcp.json",
    "**/.bashrc",
    "**/.zshrc",
    "**/.profile",
    "**/.gitconfig",
    "**/.ssh/**",
  ],
  allow_bypass: false,
  plan_exit_mode: "default",
};

const EDIT_TOOLS = new Set(["write", "edit", "apply_patch", "notebook_edit"]);
const PLAN_TOOLS = new Set(["ask_user", "exit_plan_mode", "todo_write"]);

/** The permission class of a tool: web and browser tools count as other. */
export function category(
  toolName: string,
  effectClass: z.infer<typeof EffectClass> | undefined,
): Category {
  if (EDIT_TOOLS.has(toolName)) return "edit";
  const web =
    toolName === "web_fetch" ||
    toolName === "web_search" ||
    toolName.startsWith("browser_");
  return effectClass === "read_only" && !web ? "read_only" : "other";
}

/** The first decisive step wins; in dont_ask every ask from steps 4-7 becomes deny. */
export function decide(
  permissions: Policy,
  workspace: string,
  call: PermissionCall,
): Decision {
  if (writesSelfConfig(call))
    return { decision: "deny", source: "self_config_guard" };
  const shell = shellOf(call);
  const denied = anyMatch(permissions.deny, call, workspace, shell);
  if (denied !== undefined)
    return { decision: "deny", source: "policy", rule: denied };
  if (call.mode === "plan" && call.category !== "read_only")
    return {
      decision: PLAN_TOOLS.has(call.tool) ? "allow" : "deny",
      source: "mode",
    };
  const later = laterSteps(permissions, workspace, call);
  return call.mode === "dont_ask" && later.decision === "ask"
    ? { ...later, decision: "deny" }
    : later;
}

// Step 1: config, skills, hooks and schedules load only from the host store,
// which no sandbox path reaches; .threads is where an agent would look for them. A call that
// isn't read_only and names such a path in any argument word is denied: file tools, bash, MCP,
// git and a subagent's task alike. A shell can't be told read from write, so bash is denied too.
// ponytail: a word scan, not a shell evaluator: an obfuscated name (".thr''eads") passes, and
// the host store stays unchanged regardless.
const SELF_CONFIG = /(^|\/)\.threads(\/|$)/;
const WORD_BREAK = /[\s'"`=<>|;&()]+/;

function writesSelfConfig(call: PermissionCall): boolean {
  return (
    call.category !== "read_only" &&
    strings(call.input).some((s) =>
      s.split(WORD_BREAK).some((w) => SELF_CONFIG.test(w)),
    )
  );
}

function strings(value: unknown): readonly string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(strings);
  if (typeof value === "object" && value !== null)
    return Object.values(value).flatMap(strings);
  return [];
}

function laterSteps(
  permissions: Policy,
  workspace: string,
  call: PermissionCall,
): Decision {
  const shell = shellOf(call);
  const path = isFileTool(call.tool)
    ? workspacePath(workspace, call.input)
    : undefined;
  const writes = call.category === "edit" && path !== undefined;
  if (writes && permissions.protected_paths.some((g) => globMatches(g, path)))
    return { decision: "ask", source: "protected_path" };
  const asked = anyMatch(permissions.ask, call, workspace, shell);
  if (asked !== undefined)
    return { decision: "ask", source: "policy", rule: asked };
  const allowed = allMatch(permissions.allow, call, workspace, shell);
  if (allowed !== undefined)
    return { decision: "allow", source: "policy", rule: allowed };
  const inside = !isFileTool(call.tool) || path !== undefined;
  return {
    decision: modeDefault(call.mode, call.category, inside),
    source: "mode",
  };
}

type Column = "read_in" | "read_out" | "edit_in" | "other";

function column(category: Category, inside: boolean): Column {
  if (category === "read_only") return inside ? "read_in" : "read_out";
  return category === "edit" && inside ? "edit_in" : "other";
}

/** Step 7, the mode table. */
function modeDefault(
  mode: Mode,
  category: Category,
  inside: boolean,
): Decision["decision"] {
  const col = column(category, inside);
  if (col === "read_in") return "allow";
  switch (mode) {
    case "plan":
      return col === "read_out" ? "ask" : "deny";
    case "dont_ask":
      return "deny";
    case "default":
      return "ask";
    case "accept_edits":
      return col === "edit_in" ? "allow" : "ask";
    case "bypass":
      return "allow";
    default:
      return assertNever(mode);
  }
}

const RANK = { allow: 0, ask: 1, deny: 2 } as const;

/**
 * A handoff target's decision capped by the principal and host ceiling: the
 * ceiling decides in its own mode, the stricter wins, and a tie reports the target's.
 */
export function decideCapped(
  target: Policy,
  ceiling: Policy,
  workspace: string,
  call: PermissionCall,
): Decision {
  const own = decide(target, workspace, call);
  const capped = decide(ceiling, workspace, { ...call, mode: ceiling.mode });
  return RANK[capped.decision] > RANK[own.decision] ? capped : own;
}

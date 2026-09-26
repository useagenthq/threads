import { posix } from "node:path";
import {
  type ParsedShell,
  parseShell,
  type SimpleCommand,
  specTokens,
} from "./shell";

// `tool` or `tool(specifier)`, matched per tool family.

export type Call = {
  readonly tool: string;
  readonly input: Readonly<Record<string, unknown>>;
};

type Rule = {
  readonly text: string;
  readonly tool: string;
  readonly wildcard: boolean;
  readonly spec?: string;
};

type ParsedRule =
  | { readonly ok: true; readonly value: Rule }
  | { readonly ok: false; readonly error: string };

const FILE_TOOLS = new Set([
  "read",
  "write",
  "edit",
  "apply_patch",
  "notebook_edit",
  "ls",
  "glob",
  "grep",
]);

function parseRule(text: string): ParsedRule {
  const found = /^([a-z][a-z0-9_]{0,127})(\*?)(?:\(([\s\S]+)\))?$/.exec(text);
  if (found === null || found[0] !== text)
    return { ok: false, error: `'${text}' is not tool or tool(specifier)` };
  const [, tool = "", star = "", spec] = found;
  if (spec !== undefined && star === "*")
    return {
      ok: false,
      error: `'${text}': a tool pattern takes no specifier`,
    };
  if (spec !== undefined) {
    const error = specError(tool, spec);
    if (error !== undefined) return { ok: false, error: `'${text}': ${error}` };
  }
  return {
    ok: true,
    value: {
      text,
      tool,
      wildcard: star === "*",
      ...(spec === undefined ? {} : { spec }),
    },
  };
}

export function permissionRuleError(text: string): string | undefined {
  const parsed = parseRule(text);
  return parsed.ok ? undefined : parsed.error;
}

function specError(tool: string, spec: string): string | undefined {
  if (tool === "bash") {
    const parsed = parseShell(spec.endsWith(":*") ? spec.slice(0, -2) : spec);
    return parsed.unparseable || parsed.commands.length !== 1
      ? "a bash specifier is one plain simple command"
      : undefined;
  }
  if (FILE_TOOLS.has(tool) || tool === "spawn_agent" || tool === "handoff")
    return undefined;
  if (tool === "web_fetch" || tool.startsWith("browser_"))
    return /^domain:(\*\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*$/.exec(spec)?.[0] ===
      spec
      ? undefined
      : "expected domain:host or domain:*.host";
  return "this tool takes no specifier";
}

function toolMatches(rule: Rule, tool: string): boolean {
  return rule.wildcard ? tool.startsWith(rule.tool) : rule.tool === tool;
}

export function isFileTool(tool: string): boolean {
  return FILE_TOOLS.has(tool);
}

/**
 * The call's path relative to the workspace, `.` and `..` resolved; undefined when it has no
 * string path or lies outside the workspace (then no relative rule matches it).
 */
// ponytail: symlinks are resolved in the sandbox (realpath) by the caller, not here.
export function workspacePath(
  workspace: string,
  input: Readonly<Record<string, unknown>>,
): string | undefined {
  // Absent is the tools' default: the workspace root (ls, glob, grep).
  const path = input["path"] ?? ".";
  if (typeof path !== "string") return undefined;
  const relative = posix.relative(workspace, posix.resolve(workspace, path));
  return relative.startsWith("..") || posix.isAbsolute(relative)
    ? undefined
    : relative;
}

const globCache = new Map<string, RegExp>();

/**
 * A gitignore(5) glob. A slash at the start or in the middle anchors it to the workspace root;
 * otherwise it matches at any depth. `**` spans directories. A pattern that matches a
 * directory covers everything under it, and a trailing slash names a directory, so allow and
 * deny read the same path shape the same way.
 */
export function globMatches(glob: string, path: string): boolean {
  let re = globCache.get(glob);
  if (re === undefined) {
    const body = glob.endsWith("/") ? glob.slice(0, -1) : glob;
    const anchored = body.includes("/")
      ? body.replace(/^\//, "")
      : `**/${body}`;
    re = new RegExp(`^${globSource(anchored)}$`);
    globCache.set(glob, re);
  }
  const test = re;
  const parts = path.split("/");
  return parts.some((_, i) => test.test(parts.slice(0, i + 1).join("/")));
}

function globSource(glob: string): string {
  let out = "";
  for (let i = 0; i < glob.length; i += 1) {
    const ch = glob[i] ?? "";
    if (glob.startsWith("**/", i)) {
      out += "(?:.*/)?";
      i += 2;
    } else if (glob.startsWith("**", i)) {
      out += ".*";
      i += 1;
    } else if (ch === "*") out += "[^/]*";
    else if (ch === "?") out += "[^/]";
    else out += ch.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  }
  return out;
}

function domainMatches(spec: string, url: unknown): boolean {
  if (!spec.startsWith("domain:") || typeof url !== "string") return false;
  let host: string;
  try {
    host = new URL(url).hostname;
  } catch {
    return false;
  }
  const want = spec.slice("domain:".length);
  return want.startsWith("*.") ? host.endsWith(want.slice(1)) : host === want;
}

/** A bash specifier against one simple command: `prefix:*` or the exact token sequence. */
function bashMatches(spec: string, command: SimpleCommand): boolean {
  const prefix = spec.endsWith(":*");
  const want = specTokens(prefix ? spec.slice(0, -2) : spec);
  if (!prefix && want.length !== command.tokens.length) return false;
  return (
    want.length > 0 &&
    want.length <= command.tokens.length &&
    want.every((token, i) => command.tokens[i] === token)
  );
}

/** The rule's leading tokens appear as consecutive words of an unparseable command. */
function rawMatches(spec: string, words: readonly string[]): boolean {
  const want = specTokens(spec.replace(/:\*$/, ""));
  if (want.length === 0) return false;
  return words.some((_, i) => want.every((token, j) => words[i + j] === token));
}

function specMatches(
  rule: Rule,
  spec: string,
  call: Call,
  workspace: string,
): boolean {
  if (isFileTool(call.tool)) {
    const path = workspacePath(workspace, call.input);
    return path !== undefined && globMatches(spec, path);
  }
  const { url, agent } = call.input;
  if (call.tool === "web_fetch" || call.tool.startsWith("browser_"))
    return domainMatches(spec, url);
  if (rule.tool === "spawn_agent" || rule.tool === "handoff")
    return agent === spec;
  return false;
}

/** `bash(*)`: every command, parsed or not; the sandbox is the boundary. */
function anyCommand(rule: Rule): boolean {
  return rule.tool === "bash" && rule.spec === "*";
}

/**
 * Deny and ask semantics: a bash rule matches if any simple command matches it, at any depth,
 * or, for an unparseable command, if its words appear anywhere in the raw text.
 */
export function anyMatch(
  rules: readonly string[],
  call: Call,
  workspace: string,
  shell: ParsedShell | undefined,
): string | undefined {
  return rules.find((text) => {
    const parsed = parseRule(text);
    if (!parsed.ok) return false;
    const rule = parsed.value;
    if (!toolMatches(rule, call.tool)) return false;
    if (rule.spec === undefined) return true;
    if (call.tool !== "bash")
      return specMatches(rule, rule.spec, call, workspace);
    if (anyCommand(rule)) return true;
    const spec = rule.spec;
    if (shell === undefined) return false;
    return (
      shell.commands.some((c) => bashMatches(spec, c)) ||
      (shell.unparseable && rawMatches(spec, shell.words))
    );
  });
}

/**
 * Allow semantics: a bash call is allowed only when it parses and every simple command matches
 * an allow rule and is allowable (see SimpleCommand). Returns the first command's rule.
 * `bash(*)` is the one exception: it matches every command.
 */
export function allMatch(
  rules: readonly string[],
  call: Call,
  workspace: string,
  shell: ParsedShell | undefined,
): string | undefined {
  if (call.tool !== "bash") return anyMatch(rules, call, workspace, shell);
  const anything = rules.find((text) => {
    const parsed = parseRule(text);
    return parsed.ok && anyCommand(parsed.value);
  });
  if (anything !== undefined) return anything;
  if (shell === undefined || shell.unparseable || shell.commands.length === 0)
    return undefined;
  const matched = shell.commands.map((command) =>
    command.allowable
      ? rules.find((text) => {
          const parsed = parseRule(text);
          if (!parsed.ok) return false;
          const rule = parsed.value;
          if (!toolMatches(rule, call.tool)) return false;
          return rule.spec === undefined || bashMatches(rule.spec, command);
        })
      : undefined,
  );
  return matched.every((r) => r !== undefined) ? matched[0] : undefined;
}

export function shellOf(call: Call): ParsedShell | undefined {
  const { command } = call.input;
  return call.tool === "bash" && typeof command === "string"
    ? parseShell(command)
    : undefined;
}

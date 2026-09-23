import type {
  AllowOrDeny,
  HookName,
  HookReturns,
  StopOrContinue,
  ToolDecision,
} from "./types";

// A hook's return value is checked before it becomes an event: hooks are trusted code, but a
// value the type system never saw (plain JS, a bad cast) must not reach the log. Anything else
// counts as the hook failing, which denies what a gating hook gates.

type Rec = Readonly<Record<string, unknown>>;

const isRecord = (v: unknown): v is Rec =>
  typeof v === "object" && v !== null && !Array.isArray(v);

function strings(v: unknown): readonly string[] | undefined {
  return Array.isArray(v) && v.every((x) => typeof x === "string")
    ? v.filter((x) => typeof x === "string")
    : undefined;
}

const text = (r: Rec, key: string): string | undefined =>
  typeof r[key] === "string" ? r[key] : undefined;

/** `{decision: deny, reason}` with a string reason, else undefined. */
function deny(r: Rec): { decision: "deny"; reason: string } | undefined {
  const reason = text(r, "reason");
  return r["decision"] === "deny" && reason !== undefined
    ? { decision: "deny", reason }
    : undefined;
}

/** `{decision: go}` with optional string injections. */
function withInjections<const D extends string>(
  r: Rec,
  go: D,
): { decision: D; injections: readonly string[] } | undefined {
  if (r["decision"] !== go) return undefined;
  const injections =
    r["injections"] === undefined ? [] : strings(r["injections"]);
  return injections === undefined ? undefined : { decision: go, injections };
}

function allowOrDeny(v: unknown): AllowOrDeny | undefined {
  if (!isRecord(v)) return undefined;
  return v["decision"] === "allow" ? { decision: "allow" } : deny(v);
}

function stopOrContinue(v: unknown): StopOrContinue | undefined {
  if (!isRecord(v)) return undefined;
  if (v["decision"] === "stop") return { decision: "stop" };
  const reason = text(v, "reason");
  return v["decision"] === "continue" && reason !== undefined
    ? { decision: "continue", reason }
    : undefined;
}

function tool(v: unknown): ToolDecision | undefined {
  if (!isRecord(v)) return undefined;
  if (v["decision"] === "allow") return { decision: "allow" };
  if (v["decision"] !== "ask") return deny(v);
  const rule = v["rule"];
  if (rule === undefined) return { decision: "ask" };
  return typeof rule === "string" ? { decision: "ask", rule } : undefined;
}

function guided(
  v: Rec,
): { decision: "guide"; text: string } | { decision: "proceed" } | undefined {
  if (v["decision"] === "proceed") return { decision: "proceed" };
  const guide = text(v, "text");
  return v["decision"] === "guide" && guide !== undefined
    ? { decision: "guide", text: guide }
    : undefined;
}

const isSpan = (s: unknown): s is { start: number; end: number } =>
  isRecord(s) &&
  Number.isInteger(s["start"]) &&
  Number.isInteger(s["end"]) &&
  Object.keys(s).length === 2;

function afterModel(v: unknown): HookReturns["after_model"] | undefined {
  if (!isRecord(v)) return undefined;
  const reason = text(v, "reason");
  if (v["decision"] === "retry" && reason !== undefined)
    return { decision: "retry", reason };
  return guided(v) ?? deny(v);
}

function toolResult(v: unknown): HookReturns["before_tool_result"] | undefined {
  if (!isRecord(v)) return undefined;
  if (v["decision"] === "proceed") return { decision: "proceed" };
  return v["decision"] === "redact" ? spans(v) : deny(v);
}

function spans(v: Rec): HookReturns["before_tool_result"] | undefined {
  const list = v["spans"];
  if (!Array.isArray(list) || list.length === 0) return undefined;
  const checked = list.filter(isSpan);
  return checked.length === list.length
    ? {
        decision: "redact",
        spans: checked.map((s) => ({ start: s.start, end: s.end })),
      }
    : undefined;
}

const some = <T>(x: T | undefined): { readonly value: T } | undefined =>
  x === undefined ? undefined : { value: x };
const nothing = (): { readonly value: undefined } => ({ value: undefined });

/** A checked return: present when the value is one the hook may return. */
export type Checked<K extends HookName> =
  | { readonly value: HookReturns[K] }
  | undefined;

const CHECKS: { readonly [K in HookName]: (v: unknown) => Checked<K> } = {
  session_start: (v) => some(strings(v)),
  session_end: nothing,
  before_input: (v) =>
    some(isRecord(v) ? (withInjections(v, "allow") ?? deny(v)) : undefined),
  before_model: (v) =>
    some(isRecord(v) ? (withInjections(v, "proceed") ?? deny(v)) : undefined),
  after_model: (v) => some(afterModel(v)),
  before_tool: (v) => some(tool(v)),
  permission_request: (v) => some(tool(v)),
  permission_denied: nothing,
  after_tool: (v) => some(strings(v)),
  before_tool_result: (v) => some(toolResult(v)),
  after_tool_batch: (v) => some(strings(v)),
  before_compact: (v) => some(isRecord(v) ? (guided(v) ?? deny(v)) : undefined),
  after_compact: (v) => some(strings(v)),
  on_stop: (v) => some(stopOrContinue(v)),
  stop_failure: nothing,
  subagent_start: (v) => some(allowOrDeny(v)),
  subagent_stop: (v) => some(stopOrContinue(v)),
  before_model_switch: (v) => some(allowOrDeny(v)),
  after_model_switch: nothing,
  notification: nothing,
};

/** The checked decision, or undefined when the value is not one this hook may return. */
export function checkReturn<K extends HookName>(
  hook: K,
  value: unknown,
): Checked<K> {
  return CHECKS[hook](value);
}

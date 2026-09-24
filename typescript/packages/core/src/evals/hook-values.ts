import type { HookName } from "../hooks/types";
import type { HookRecord } from "./files";

// A recorded hook outcome as the value the hook returns (hooks/types.ts HookReturns), so the
// loop appends the same hook_decision and injected events again. One entry per hook: the map's
// type makes a new hook a compile error until it has one.

type Value = (
  r: HookRecord,
  reason: string,
  texts: readonly string[],
) => unknown;

const decided: Value = (r, reason) => ({ decision: r.decision, reason });
const injections: Value = (_r, _reason, texts) => texts;
const nothing: Value = () => undefined;
const guided: Value = (r, reason) =>
  r.decision === "guide"
    ? { decision: "guide", text: reason }
    : decided(r, reason, []);

/** A tool decision; an ask records its rule as the reason. */
const asked: Value = (r, reason) =>
  r.decision === "ask"
    ? { decision: "ask", ...(r.reason === undefined ? {} : { rule: reason }) }
    : decided(r, reason, []);

const VALUES: { readonly [K in HookName]: Value } = {
  session_start: injections,
  after_tool_batch: injections,
  after_compact: injections,
  after_tool: (_r, reason) => [reason],
  before_input: (r, reason, texts) =>
    r.decision === "deny"
      ? { decision: "deny", reason }
      : { decision: "allow", injections: texts },
  before_model: (r, reason, texts) =>
    r.decision === "deny"
      ? { decision: "deny", reason }
      : { decision: "proceed", injections: texts },
  after_model: guided,
  before_compact: guided,
  before_tool: asked,
  permission_request: asked,
  before_tool_result: (r, reason) =>
    r.decision === "redact"
      ? { decision: "redact", spans: r.spans ?? [] }
      : decided(r, reason, []),
  on_stop: decided,
  subagent_start: decided,
  subagent_stop: decided,
  before_model_switch: decided,
  permission_denied: nothing,
  stop_failure: nothing,
  session_end: nothing,
  notification: nothing,
  after_model_switch: nothing,
};

/** What the hook returns to reproduce `r`: a decision, its reason and its injected text. */
export function hookValue(r: HookRecord): unknown {
  const texts = r.injected.flatMap((i) =>
    i.text === undefined ? [] : [i.text],
  );
  return VALUES[r.hook](r, r.reason ?? "", texts);
}

import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import type { LoopExtension, ToolDecision } from "../hooks/types";
import type { KnownEvent } from "../log";
import { draft } from "./drafts";
import { decision, defining, observe, recorded, run } from "./hooks";
import type { Session } from "./session";
import type { Authorization, Halt } from "./types";

// The permission fold for one call:
// the policy's decision, then before_tool hooks, strictest wins (deny > ask > allow). An ask
// goes to permission_request hooks, the programmatic approver. Each hook decision is recorded
// before the permission_decision, and read back instead of re-running the hook.

type Call = EventOf<"tool_call">;
type Verdict = {
  readonly decision: "allow" | "deny" | "ask";
  readonly reason?: string;
};

const SEVERITY = { allow: 0, ask: 1, deny: 2 } as const;

/** A decision a hook recorded (or failed with) as the verdict it stands for. */
function verdictOf(d: EventOf<"hook_decision">["data"]): Verdict {
  const reason = d.reason === undefined ? {} : { reason: d.reason };
  switch (d.decision) {
    case "allow":
      return { decision: "allow" };
    case "ask":
      return { decision: "ask", ...reason };
    default:
      // deny, and failed: a gating hook that throws or times out denies (item 3).
      return { decision: "deny", ...reason };
  }
}

function recordedFor(value: ToolDecision): {
  readonly decided: "allow" | "deny" | "ask";
  readonly reason?: string;
} {
  switch (value.decision) {
    case "allow":
      return { decided: "allow" };
    case "deny":
      return { decided: "deny", reason: value.reason };
    case "ask":
      return value.rule === undefined
        ? { decided: "ask" }
        : { decided: "ask", reason: value.rule };
    default:
      return assertNever(value);
  }
}

const isHalt = (v: Verdict | Halt | undefined): v is Halt =>
  v !== undefined && "code" in v;

/** Runs the hook and records its decision; the verdict is what the recorded decision means. */
async function decideNow(
  s: Session,
  ext: LoopExtension,
  hook: "before_tool" | "permission_request",
  call: Call,
): Promise<Verdict | Halt> {
  const key = { call_id: call.data.call_id };
  const out = await run(ext, hook, [call.data], key.call_id);
  const d =
    out.kind === "failed"
      ? { decided: "failed" as const, reason: out.reason }
      : recordedFor(out.value);
  const stopped = s.append(decision(ext.name, hook, d.decided, key, d.reason));
  if (stopped !== undefined) return stopped;
  return verdictOf({
    extension: ext.name,
    hook,
    decision: d.decided,
    ...(d.reason === undefined ? {} : { reason: d.reason }),
  });
}

/** Every extension's decision on `hook` for this call, strictest first; deny absorbs. */
async function hookVerdict(
  s: Session,
  hook: "before_tool" | "permission_request",
  call: Call,
): Promise<Verdict | Halt | undefined> {
  const key = { call_id: call.data.call_id };
  const window: readonly KnownEvent[] = s.events.filter(
    (e) => e.seq > call.seq,
  );
  let strictest: Verdict | undefined;
  for (const ext of defining(s, hook)) {
    const prior = recorded(window, ext.name, hook, key);
    const verdict =
      prior === undefined
        ? await decideNow(s, ext, hook, call)
        : verdictOf(prior.data);
    if (isHalt(verdict)) return verdict;
    if (
      strictest === undefined ||
      SEVERITY[verdict.decision] > SEVERITY[strictest.decision]
    )
      strictest = verdict;
    if (strictest.decision === "deny") break;
  }
  return strictest;
}

/** Folds, records the permission_decision, and tells permission_denied observers. */
export async function authorize(
  s: Session,
  call: Call,
): Promise<Halt | undefined> {
  const hooked = await hookVerdict(s, "before_tool", call);
  if (isHalt(hooked)) return hooked;
  let final = folded(s.config.authorize(call, s.fold), hooked);
  // The self-config guard is never an ask a programmatic approver can answer.
  if (final.decision === "ask" && final.source !== "self_config_guard") {
    const answered = await hookVerdict(s, "permission_request", call);
    if (isHalt(answered)) return answered;
    if (answered !== undefined && answered.decision !== "ask")
      final = { ...answered, source: "hook" };
  }
  const stopped = s.append(
    draft.permission({
      call_id: call.data.call_id,
      decision: final.decision,
      source: final.source,
      ...(final.rule_id === undefined ? {} : { rule_id: final.rule_id }),
      ...(final.reason === undefined ? {} : { reason: final.reason }),
    }),
  );
  if (stopped !== undefined || final.decision !== "deny") return stopped;
  return observe(s, "permission_denied", [call.data], {
    call_id: call.data.call_id,
  });
}

type Final = Authorization & { readonly reason?: string };

/**
 * deny > ask > allow. A policy deny stands. A hook decision at least as strict as the
 * policy's is the recorded one (source hook); a hook can never loosen the policy.
 */
function folded(policy: Authorization, hook: Verdict | undefined): Final {
  if (policy.decision === "deny" || hook === undefined) return policy;
  return SEVERITY[hook.decision] >= SEVERITY[policy.decision]
    ? { ...hook, source: "hook" }
    : policy;
}

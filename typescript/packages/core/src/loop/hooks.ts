import type { EventOf } from "../fold/state";
import { invoke, type Outcome } from "../hooks/invoke";
import type {
  HookArgs,
  HookName,
  HookReturns,
  LoopExtension,
} from "../hooks/types";
import type { KnownEvent } from "../log";
import type { EventDraft } from "../store";
import { draft } from "./drafts";
import type { Session } from "./session";
import type { Halt } from "./types";

// Shared plumbing for hook points: hook order, the recorded decision
// that stands in for a hook on replay, and the observation rule.

type Decision = EventOf<"hook_decision">;
type Data = Decision["data"];
/** What ties a decision to the work it gates. */
export type HookKey = Pick<
  Data,
  "request_event_id" | "call_id" | "input_event_id"
>;

const AFTER: ReadonlySet<HookName> = new Set([
  "after_model",
  "after_tool",
  "after_tool_batch",
  "after_compact",
  "after_model_switch",
]);

/** Extensions defining `hook`: before* in declaration order, after* in reverse (item 5). */
export function defining(s: Session, hook: HookName): readonly LoopExtension[] {
  const all = (s.config.extensions ?? []).filter(
    (e) => e.hooks[hook] !== undefined,
  );
  return AFTER.has(hook) ? all.toReversed() : all;
}

/** This extension's recorded decision on `hook` for `key` among `window`, if any. */
export function recorded(
  window: readonly KnownEvent[],
  ext: string,
  hook: HookName,
  key: HookKey = {},
): Decision | undefined {
  return window.findLast(
    (e): e is Decision =>
      e.type === "hook_decision" &&
      e.data.hook === hook &&
      e.data.extension === ext &&
      e.data.request_event_id === key.request_event_id &&
      e.data.call_id === key.call_id &&
      e.data.input_event_id === key.input_event_id,
  );
}

export function decision(
  ext: string,
  hook: HookName,
  decided: Data["decision"],
  key: HookKey = {},
  reason?: string,
): EventDraft {
  return {
    type: "hook_decision",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      extension: ext,
      hook,
      decision: decided,
      ...(reason === undefined ? {} : { reason }),
      ...key,
    },
  };
}

/** Hook-added text: model-visible, always as untrusted reference (C6, invariant 6). */
export function injection(ext: string, text: string): EventDraft {
  return draft.injected({
    source: "hook",
    trust: "untrusted_reference",
    origin: { id: ext },
    text,
  });
}

/** A hook's own trusted instruction (on_stop continue, after_model guide; ). */
export function instruction(ext: string, text: string): EventDraft {
  return draft.injected({
    source: "hook",
    trust: "trusted_instruction",
    origin: { id: ext },
    text,
  });
}

export async function run<K extends HookName>(
  ext: LoopExtension,
  hook: K,
  args: HookArgs[K],
  callId?: string,
): Promise<Outcome<K>> {
  const out = await invoke(ext, hook, args, callId);
  if (out === undefined) throw new Error(`${ext.name} defines no ${hook}`);
  return out;
}

/**
 * An observation hook: it can't change execution. A failure is recorded and ignored, and an
 * annotation (after_tool) is recorded as `annotate`; nothing else leaves a trace.
 */
export async function observe<K extends HookName>(
  s: Session,
  hook: K,
  args: HookArgs[K],
  key: HookKey = {},
): Promise<Halt | undefined> {
  for (const ext of defining(s, hook)) {
    const out = await run(ext, hook, args, key.call_id);
    const notes = out.kind === "ok" ? annotations(out.value) : [];
    const stopped =
      out.kind === "failed"
        ? s.append(decision(ext.name, hook, "failed", key, out.reason))
        : notes.length > 0
          ? s.append(
              decision(ext.name, hook, "annotate", key, notes.join("\n")),
            )
          : undefined;
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}

function annotations(value: HookReturns[HookName]): readonly string[] {
  return Array.isArray(value)
    ? value.filter((x): x is string => typeof x === "string")
    : [];
}

/**
 * A context hook (session_start, after_tool_batch, after_compact): each extension's
 * injections are recorded with its `proceed`, in one append. A failure is recorded as
 * `failed` and reported, so the caller denies the step it feeds.
 */
export async function context(
  s: Session,
  hook: "session_start" | "after_tool_batch" | "after_compact",
  args: HookArgs[typeof hook],
  window: readonly KnownEvent[] = [],
): Promise<Halt | "failed" | undefined> {
  for (const ext of defining(s, hook)) {
    if (recorded(window, ext.name, hook) !== undefined) continue;
    const out = await run(ext, hook, args);
    if (out.kind === "failed") {
      const stopped = s.append(
        decision(ext.name, hook, "failed", {}, out.reason),
      );
      return stopped ?? "failed";
    }
    const stopped = s.append(
      decision(ext.name, hook, "proceed"),
      ...out.value.map((text) => injection(ext.name, text)),
    );
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}

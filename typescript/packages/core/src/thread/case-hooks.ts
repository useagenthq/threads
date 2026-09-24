import type { z } from "zod";
import type { ExtensionScript, HookRecord, RecallRecord } from "../evals/files";
import { HOOK_KINDS } from "../evals/kinds";
import type { EventOf } from "../fold/state";
import type { InjectedData, KnownEvent } from "../log";

// extensions.json (spec lane 22, A.2): the turn's hook decisions and recall with their full
// outcomes, which the offline rerun replays through stand-in extensions, since it loads no user
// code. Observation records are keyed by the call they belong to.

type Decision = EventOf<"hook_decision">;
type Injected = z.infer<typeof InjectedData>;

/** What sets off each keyed observation hook, counted within the turn. */
const TRIGGERS: Readonly<Record<string, ReadonlySet<string>>> = {
  after_model_switch: new Set(["settings_changed"]),
  notification: new Set([
    "parked",
    "retry_scheduled",
    "budget_exceeded",
    "agent_finished",
  ]),
};

/** The call an observation record belongs to, from events the rerun reproduces. */
function keyOf(
  turn: readonly KnownEvent[],
  at: number,
  d: Decision,
): HookRecord["at"] {
  const { hook, call_id } = d.data;
  if (HOOK_KINDS[hook] === "recorded") return undefined;
  if (call_id !== undefined) return { call_id };
  const trigger = TRIGGERS[hook];
  if (trigger === undefined) return { occurrence: 1 };
  const count = turn.slice(0, at).filter((e) => trigger.has(e.type)).length;
  return { occurrence: Math.max(count, 1) };
}

/** The injected{source: hook} events this decision produced: its own append, and a guide's text. */
function injectedBy(
  turn: readonly KnownEvent[],
  at: number,
  d: Decision,
): readonly Injected[] {
  const own = (e: KnownEvent | undefined): e is EventOf<"injected"> =>
    e?.type === "injected" &&
    e.data.source === "hook" &&
    e.data.origin.id === d.data.extension;
  const out: Injected[] = [];
  for (let i = at + 1; own(turn[i]); i += 1) {
    const e = turn[i];
    if (own(e)) out.push(e.data);
  }
  if (out.length > 0 || d.data.decision !== "guide") return out;
  // after_model's guide is appended after the withheld calls' results, not with the decision.
  const guide = turn
    .slice(at + 1)
    .find((e) => own(e) && e.data.text === d.data.reason);
  return own(guide) ? [guide.data] : [];
}

/** before_tool_result redact: the spans its context_edited carries. */
function spansOf(turn: readonly KnownEvent[], at: number): HookRecord["spans"] {
  const next = turn[at + 1];
  if (next?.type !== "context_edited") return undefined;
  const edit = next.data.edits[0];
  return edit?.action === "redact" ? [...edit.spans] : undefined;
}

function hookRecords(turn: readonly KnownEvent[]): readonly HookRecord[] {
  const seen = new Map<string, number>();
  return turn.flatMap((e, i): HookRecord[] => {
    if (e.type !== "hook_decision") return [];
    const { extension, hook, decision, reason } = e.data;
    const key = `${extension}\n${hook}`;
    const occurrence = (seen.get(key) ?? 0) + 1;
    seen.set(key, occurrence);
    const at = keyOf(turn, i, e);
    const spans = decision === "redact" ? spansOf(turn, i) : undefined;
    return [
      {
        extension,
        hook,
        occurrence,
        ...(at === undefined ? {} : { at }),
        decision,
        ...(reason === undefined ? {} : { reason }),
        injected: [...injectedBy(turn, i, e)],
        ...(spans === undefined ? {} : { spans }),
      },
    ];
  });
}

/** Each recalling call's injected items: the memory or knowledge a search brought back. */
function recallRecords(turn: readonly KnownEvent[]): readonly RecallRecord[] {
  const seen = new Map<string, number>();
  const records: RecallRecord[] = [];
  let open: RecallRecord | undefined;
  for (const e of turn) {
    const source =
      e.type === "injected" &&
      (e.data.source === "memory" || e.data.source === "knowledge")
        ? e.data.source
        : undefined;
    if (e.type === "tool_result") open = undefined;
    if (source === undefined || e.type !== "injected") continue;
    if (open?.source !== source) {
      const occurrence = (seen.get(source) ?? 0) + 1;
      seen.set(source, occurrence);
      open = { source, occurrence, items: [] };
      records.push(open);
    }
    open.items.push(e.data);
  }
  return records;
}

/** The turn's extensions.json, or undefined when no hook decided and nothing was recalled. */
export function extensionScript(
  turn: readonly KnownEvent[],
): ExtensionScript | undefined {
  const hooks = hookRecords(turn);
  const recall = recallRecords(turn);
  return hooks.length === 0 && recall.length === 0
    ? undefined
    : { hooks: [...hooks], recall: [...recall] };
}

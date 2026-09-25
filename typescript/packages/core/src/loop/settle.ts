import { assertNever } from "../assert-never";
import { type EventOf, effectKey } from "../fold/state";
import type { ArtifactRef } from "../log";
import type { EventDraft } from "../store";
import { draft } from "./drafts";
import { lookedUp } from "./lookup";
import type { Session } from "./session";
import { recordOutput } from "./spill";
import { callSpec } from "./turn";
import type { Halt, ToolImpl } from "./types";

// an unknown effect is settled only with a proof its class allows;
// anything else parks for a human. Nothing is ever auto-resolved.

const DAY = 86_400_000;

type Actor = { readonly kind: "host" | "recovery" };

export async function settleUnknown(
  s: Session,
  callId: string,
  actor: Actor,
): Promise<Halt | undefined> {
  const call = s.events.findLast(
    (e) => e.type === "tool_call" && e.data.call_id === callId,
  );
  if (call?.type !== "tool_call")
    throw new Error("an effect has its tool_call");
  const spec = callSpec(s.fold, call);
  const impl = s.config.tools.get(call.data.name);
  const effectClass = spec?.effect_class;
  switch (effectClass) {
    case "idempotent":
      return withinWindow(s, callId, spec?.dedup_window_ms, impl)
        ? s.append(
            draft.effectResolved(
              {
                call_id: callId,
                outcome: "safe_to_retry",
                by: "provider_dedup",
              },
              actor,
            ),
          )
        : park(s, callId, actor);
    case "reconcilable":
      return reconcile(s, call, impl, actor);
    case "sandbox_local":
      return interrupt(s, callId, impl, actor);
    case "unguarded":
    case "read_only":
    case undefined:
      return park(s, callId, actor);
    default:
      return assertNever(effectClass);
  }
}

/**
 * provider_now − begin ≤ window − skew, measured from the EARLIEST effect_begin that may still
 * have been sent: a deduplicated re-send does not renew the provider's retention, and only a
 * not_sent proof takes an attempt out. Without a provider clock the host clock is
 * used and the skew margin doubles.
 */
function withinWindow(
  s: Session,
  callId: string,
  window: number | undefined,
  impl: ToolImpl | undefined,
): boolean {
  const first = earliestSent(s, callId);
  if (window === undefined || first === undefined) return false;
  const skew =
    s.config.skewMarginMs * (impl?.providerNow === undefined ? 2 : 1);
  const now = impl?.providerNow?.() ?? s.now();
  return now - first <= window - skew;
}

function earliestSent(s: Session, callId: string): number | undefined {
  let first: number | undefined;
  for (const e of s.events) {
    if (e.type === "effect_begin" && e.data.call_id === callId)
      first ??= e.time;
    else if (
      e.type === "effect_resolved" &&
      e.data.call_id === callId &&
      e.data.outcome === "not_sent"
    )
      first = undefined;
  }
  return first;
}

async function reconcile(
  s: Session,
  call: EventOf<"tool_call">,
  impl: ToolImpl | undefined,
  actor: Actor,
): Promise<Halt | undefined> {
  const callId = call.data.call_id;
  const contract = impl?.reconcile;
  if (contract === undefined) return park(s, callId, actor);
  const fenced = await s.fence();
  if (fenced !== undefined) return fenced;
  const answer = await lookedUp(
    () =>
      contract.lookup(effectKey(s.fold, callId, s.branchId), call.data.input),
    (reason) => ({ status: "unknown" as const, reason }),
  );
  if (answer.status === "found") {
    const shown = await recordOutput(s, callId, answer.value);
    const ref = shown.ref ?? (await s.store(shown.text, "text/plain"));
    // The resolution and its result commit together.
    return s.append(
      draft.effectResolved(
        {
          call_id: callId,
          outcome: "confirmed_success",
          by: "reconcile",
          result_ref: ref,
        },
        actor,
      ),
      terminalResult(
        callId,
        "confirmed_success",
        shown.preview,
        actor,
        shown.ref,
      ),
    );
  }
  // A not_found settles only when the tool's declared finality makes it final.
  if (answer.status === "not_found" && contract.finality === "final")
    return s.append(
      draft.effectResolved(
        { call_id: callId, outcome: "safe_to_retry", by: "reconcile" },
        actor,
      ),
    );
  return park(s, callId, actor);
}

/** sandbox_local: settled as interrupted only once the process group is confirmed gone. */
async function interrupt(
  s: Session,
  callId: string,
  impl: ToolImpl | undefined,
  actor: Actor,
): Promise<Halt | undefined> {
  const fenced = await s.fence();
  if (fenced !== undefined) return fenced;
  const gone = await impl?.terminate?.(effectKey(s.fold, callId, s.branchId));
  if (gone !== "terminated" && gone !== "already_exited")
    return park(s, callId, actor);
  return s.append(
    draft.effectResolved(
      { call_id: callId, outcome: "interrupted", by: "sandbox_terminated" },
      actor,
    ),
    terminalResult(callId, "interrupted", interruption(s, callId), actor),
  );
}

function interruption(s: Session, callId: string): string {
  const unknown = s.events.findLast(
    (e) => e.type === "effect_unknown" && e.data.call_id === callId,
  );
  const why =
    unknown?.type === "effect_unknown" && unknown.data.reason === "timeout"
      ? "timed out"
      : "the run stopped";
  return `interrupted: ${why}; the command may have partly run`;
}

type Terminal = "confirmed_success" | "interrupted" | "assume_done";

/** The tool_result a terminal resolution closes its call with; the call is never re-run. */
export function terminalResult(
  callId: string,
  outcome: Terminal,
  preview: string,
  actor: Actor,
  ref?: ArtifactRef,
): EventDraft {
  return draft.toolResult(
    {
      call_id: callId,
      is_error: outcome === "interrupted",
      origin: outcome === "interrupted" ? "interrupted" : "executed",
      preview,
      ...(ref === undefined ? {} : { ref }),
    },
    actor,
  );
}

/**
 * A recorded terminal resolution whose result never landed (a crash between them): its result
 * again, from the resolution alone.
 */
export async function resolvedResult(
  s: Session,
  resolved: EventOf<"effect_resolved">,
  actor: Actor,
): Promise<Halt | undefined> {
  const { call_id: callId, outcome, result_ref: ref } = resolved.data;
  if (outcome === "interrupted")
    return s.append(
      terminalResult(callId, outcome, interruption(s, callId), actor),
    );
  if (outcome === "assume_done")
    return s.append(
      terminalResult(callId, outcome, "assumed done by an approver", actor),
    );
  if (outcome !== "confirmed_success")
    throw new Error(`${outcome} is not terminal`);
  const bytes = await (ref === undefined
    ? undefined
    : s.artifacts.get(ref.sha256));
  if (bytes !== undefined && !bytes.ok)
    return { code: "artifact_missing", message: bytes.error.message };
  const shown = await recordOutput(
    s,
    callId,
    bytes === undefined ? "" : new TextDecoder().decode(bytes.value),
  );
  return s.append(
    terminalResult(callId, outcome, shown.preview, actor, shown.ref),
  );
}

async function park(
  s: Session,
  callId: string,
  actor: Actor,
): Promise<Halt | undefined> {
  const id = effectKey(s.fold, callId, s.branchId);
  if (s.fold.parked.some((a) => a.kind === "effect" && a.id === id))
    return undefined;
  return s.append(
    draft.parked(
      {
        address: { kind: "effect", id },
        reason: "effect_unknown",
        expires_at: s.now() + DAY,
      },
      actor,
    ),
  );
}

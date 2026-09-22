import { assertNever } from "../assert-never";
import { effectKey } from "../fold/state";
import { draft } from "./drafts";
import type { Session } from "./session";
import { toolSpec } from "./turn";
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
  const spec = toolSpec(s.fold, call.data.name);
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
      return reconcile(s, callId, impl, actor);
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
  callId: string,
  impl: ToolImpl | undefined,
  actor: Actor,
): Promise<Halt | undefined> {
  const contract = impl?.reconcile;
  if (contract === undefined) return park(s, callId, actor);
  const answer = await contract.lookup(effectKey(s.fold, callId, s.branchId));
  if (answer.status === "found") {
    const ref = s.store(answer.value, "text/plain");
    return (
      s.append(
        draft.effectResolved(
          {
            call_id: callId,
            outcome: "confirmed_success",
            by: "reconcile",
            result_ref: ref,
          },
          actor,
        ),
      ) ??
      s.append(
        draft.toolResult(
          {
            call_id: callId,
            is_error: false,
            origin: "executed",
            preview: answer.value,
          },
          actor,
        ),
      )
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
  const gone = await impl?.terminate?.(effectKey(s.fold, callId, s.branchId));
  if (gone !== "terminated" && gone !== "already_exited")
    return park(s, callId, actor);
  const unknown = s.events.findLast(
    (e) => e.type === "effect_unknown" && e.data.call_id === callId,
  );
  const why =
    unknown?.type === "effect_unknown" && unknown.data.reason === "timeout"
      ? "timed out"
      : "the run stopped";
  return (
    s.append(
      draft.effectResolved(
        { call_id: callId, outcome: "interrupted", by: "sandbox_terminated" },
        actor,
      ),
    ) ??
    s.append(
      draft.toolResult(
        {
          call_id: callId,
          is_error: true,
          origin: "interrupted",
          preview: `interrupted: ${why}; the command may have partly run`,
        },
        actor,
      ),
    )
  );
}

function park(s: Session, callId: string, actor: Actor): Halt | undefined {
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

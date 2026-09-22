import { assertNever } from "../assert-never";
import type { KnownEvent } from "../log";
import { applyAgents } from "./agents";
import {
  type EffectStatus,
  type EventLine,
  type EventOf,
  effectKey,
  type Fold,
  type Output,
  responseText,
  sameAddress,
} from "./state";

function output(data: EventOf<"output_validated">["data"]): Output {
  if (data.outcome === "rejected") return { outcome: "rejected" };
  // The schema's if/then rule requires value when accepted; the type can't see a rule.
  if (data.value === undefined)
    throw new Error("accepted output without value");
  return { outcome: "accepted", value: data.value };
}

/** Advances the fold past one event that already passed validate_next. */
export function apply(fold: Fold, line: EventLine): void {
  const { event } = line;
  fold.seq = event.seq;
  fold.epoch = event.epoch;
  fold.eventIds.add(event.event_id);
  if (line.kind === "event") applyKnown(fold, line.event);
  fold.boundaries[event.seq] =
    fold.pending.size === 0 && fold.awaiting.size === 0;
}

function applyKnown(fold: Fold, e: KnownEvent): void {
  switch (e.type) {
    case "thread_started":
    case "tools_changed":
    case "settings_changed":
    case "mode_changed":
    case "budget_exceeded":
    case "handoff":
    case "output_validated":
      applyConfig(fold, e);
      return;
    case "user_input":
    case "turn_completed":
    case "model_request":
    case "model_response":
    case "model_response_recovered":
    case "model_attempt_abandoned":
      applyTurn(fold, e);
      return;
    case "tool_call":
    case "permission_decision":
    case "approval_requested":
    case "approval_granted":
    case "approval_denied":
    case "tool_result":
    case "tool_result_late":
      applyCall(fold, e);
      return;
    case "effect_begin":
      applyEffect(fold, e, "begun");
      return;
    case "effect_commit":
      applyEffect(fold, e, "committed");
      return;
    case "effect_unknown":
      applyEffect(fold, e, "unknown");
      return;
    case "effect_resolved":
      applyEffect(fold, e, "resolved");
      return;
    case "parked":
    case "resumed":
    case "cancel_requested":
    case "cancelled":
    case "snapshot":
    case "fork":
    case "compacted":
    case "compaction_failed":
      applyControl(fold, e);
      return;
    case "agent_spawned":
    case "agent_finished":
    case "todos_updated":
    case "team_task_created":
    case "team_task_claimed":
    case "team_task_updated":
    case "team_message":
    case "channel_delivery":
    case "schedule_fired":
      applyAgents(fold, e);
      return;
    case "steer":
    case "injected":
    case "heartbeat":
    case "hook_decision":
    case "park_escalated":
    case "stop_when_idle":
    case "schedule_skipped":
    case "log_repaired":
    case "retry_scheduled":
    case "context_edited":
    case "permission_rule_added":
    case "context_preflight_blocked":
      return;
    default:
      assertNever(e);
  }
}

type ConfigEvent = EventOf<
  | "thread_started"
  | "tools_changed"
  | "settings_changed"
  | "mode_changed"
  | "budget_exceeded"
  | "handoff"
  | "output_validated"
>;

function applyConfig(fold: Fold, e: ConfigEvent): void {
  switch (e.type) {
    case "thread_started":
      fold.policy = e.data.policy;
      fold.tools = e.data.tools;
      fold.model = e.data.model;
      fold.mode = e.data.policy?.permissions?.mode ?? "default";
      return;
    case "tools_changed":
      fold.tools = e.data.tools;
      return;
    case "settings_changed":
      fold.model = e.data.settings.model;
      return;
    case "mode_changed":
      fold.mode = e.data.to;
      return;
    case "budget_exceeded":
      fold.budgetBlocked = true;
      return;
    case "handoff":
      fold.handedOff = true;
      return;
    case "output_validated":
      fold.output = output(e.data);
      return;
    default:
      assertNever(e);
  }
}

type TurnEvent = EventOf<
  | "user_input"
  | "turn_completed"
  | "model_request"
  | "model_response"
  | "model_response_recovered"
  | "model_attempt_abandoned"
>;

function applyTurn(fold: Fold, e: TurnEvent): void {
  switch (e.type) {
    case "user_input":
      fold.turnOpen = true;
      fold.budgetBlocked = false;
      return;
    case "turn_completed":
      fold.turnOpen = false;
      fold.turns += 1;
      return;
    case "model_request": {
      const compaction = e.data.purpose === "compaction";
      fold.requests.set(e.event_id, { compaction });
      fold.awaiting.add(e.event_id);
      return;
    }
    case "model_response":
    case "model_response_recovered":
      applyResponse(fold, e);
      return;
    case "model_attempt_abandoned":
      fold.awaiting.delete(e.data.request_event_id);
      return;
    default:
      assertNever(e);
  }
}

function applyResponse(
  fold: Fold,
  e: EventOf<"model_response" | "model_response_recovered">,
): void {
  const { input_tokens: input, output_tokens: output } = e.data.usage;
  fold.usage.input += input ?? 0;
  fold.usage.output += output ?? 0;
  if (input === null || output === null) fold.usage.unknown += 1;
  const requestId = e.data.request_event_id;
  fold.awaiting.delete(requestId);
  if (fold.requests.get(requestId)?.compaction === true) {
    fold.summaries.set(requestId, responseText(e.data.content));
  }
}

type CallEvent = EventOf<
  | "tool_call"
  | "permission_decision"
  | "approval_requested"
  | "approval_granted"
  | "approval_denied"
  | "tool_result"
  | "tool_result_late"
>;

function applyCall(fold: Fold, e: CallEvent): void {
  const call = fold.calls.get(e.data.call_id);
  switch (e.type) {
    case "tool_call": {
      const spec = fold.tools.find((tool) => tool.name === e.data.name);
      fold.calls.set(e.data.call_id, {
        branchId: e.branch_id,
        effectClass: spec?.effect_class,
        allowed: false,
        barrier: false,
        result: undefined,
        deferred: false,
        late: false,
      });
      fold.pending.add(e.data.call_id);
      return;
    }
    case "permission_decision":
      if (call !== undefined) call.allowed = e.data.decision === "allow";
      return;
    case "approval_requested":
      fold.approvals.set(e.data.challenge_id, {
        callId: e.data.call_id,
        argsHash: e.data.args_hash,
        consumed: false,
      });
      return;
    case "approval_granted":
    case "approval_denied": {
      const challenge = fold.approvals.get(e.data.challenge_id);
      if (challenge !== undefined) challenge.consumed = true;
      if (call !== undefined && e.type === "approval_granted")
        call.allowed = true;
      return;
    }
    case "tool_result":
      fold.pending.delete(e.data.call_id);
      if (call === undefined) return;
      call.result = e;
      call.deferred = e.data.origin === "deferred";
      return;
    case "tool_result_late":
      if (call === undefined) return;
      call.result = e;
      call.late = true;
      return;
    default:
      assertNever(e);
  }
}

function applyEffect(
  fold: Fold,
  e: EventOf<
    "effect_begin" | "effect_commit" | "effect_unknown" | "effect_resolved"
  >,
  status: EffectStatus,
): void {
  const callId = e.data.call_id;
  const key = effectKey(fold, callId, e.branch_id);
  fold.effects.set(key, { callId, status });
}

type ControlEvent = EventOf<
  | "parked"
  | "resumed"
  | "cancel_requested"
  | "cancelled"
  | "snapshot"
  | "fork"
  | "compacted"
  | "compaction_failed"
>;

function applyControl(fold: Fold, e: ControlEvent): void {
  switch (e.type) {
    case "parked":
      fold.parked.push(e.data.address);
      return;
    case "resumed": {
      const at = fold.parked.findIndex((a) => sameAddress(a, e.data.address));
      if (at !== -1) fold.parked.splice(at, 1);
      return;
    }
    case "cancel_requested":
      fold.cancelScopes.set(e.event_id, e.data.scope);
      for (const call of fold.calls.values()) call.barrier = true;
      return;
    case "cancelled": {
      const scope = fold.cancelScopes.get(e.data.request_event_id);
      fold.cancelled ||= scope === "thread" || scope === "tree";
      return;
    }
    case "snapshot":
      fold.snapshots.push({
        seq: e.seq,
        eventId: e.event_id,
        quiescent: isQuiescent(fold),
        expiresAt: e.data.expires_at,
      });
      return;
    case "fork":
      // The last fork on the resolved chain is this branch's own.
      fold.repair = e.data.reason === "repair";
      return;
    case "compacted":
      fold.ranges.push([e.data.from_seq, e.data.to_seq]);
      fold.compactionFailures = 0;
      return;
    case "compaction_failed":
      fold.compactionFailures += 1;
      return;
    default:
      assertNever(e);
  }
}

/** C4 without the expiry, which depends on the reader's clock. */
function isQuiescent(fold: Fold): boolean {
  const settled = [...fold.effects.values()].every(
    (effect) => effect.status === "committed" || effect.status === "resolved",
  );
  return (
    !fold.turnOpen &&
    fold.pending.size === 0 &&
    fold.parked.length === 0 &&
    settled
  );
}

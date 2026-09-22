import { assertNever } from "../assert-never";
import type { EventLine, Fold } from "../fold/state";
import type { KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import { checkAgentFinished, checkTask, checkUnique } from "./agents";
import {
  checkApproval,
  checkCancelled,
  checkContextEdit,
  checkEffect,
  checkLateResult,
  checkToolCall,
  checkToolResult,
} from "./calls";
import { checkModeChanged, checkOutput, checkToolsChanged } from "./config";
import {
  checkCompacted,
  checkInput,
  checkModelRequest,
  checkSettings,
} from "./turns";
import type { Violation } from "./violation";

/**
 * The semantic rules that need earlier events (spec/schema/README.md, "Semantic rules" 6-13
 * and 17-28), checked against the fold before `line` is applied. Rules 1-4 are the chain's,
 * 5 is the parser's, and 14-16 need rendering or a fork request.
 */
export function validateNext(
  fold: Fold,
  line: EventLine,
): Result<void, LogError> {
  const { event } = line;
  if (event.epoch < fold.epoch)
    return fail(`epoch ${event.epoch} is below ${fold.epoch}`, event.seq);
  if (fold.eventIds.has(event.event_id))
    return fail(`event_id ${event.event_id} repeats on the chain`, event.seq);
  if (line.kind === "unknown_event") return ok(undefined);
  const violation = check(fold, line.event);
  return violation === undefined
    ? ok(undefined)
    : err(logError(violation.code, violation.message, event.seq));
}

function fail(
  message: string,
  seq: number,
): { readonly ok: false; readonly error: LogError } {
  return err(logError("invalid_transition", message, seq));
}

function check(fold: Fold, e: KnownEvent): Violation {
  switch (e.type) {
    case "tools_changed":
      return checkToolsChanged(e);
    case "user_input":
    case "steer":
      return checkInput(fold, e);
    case "model_request":
      return checkModelRequest(fold);
    case "settings_changed":
      return checkSettings(fold, e);
    case "compacted":
      return checkCompacted(fold, e);
    case "tool_call":
      return checkToolCall(fold, e);
    case "tool_result":
      return checkToolResult(fold, e);
    case "tool_result_late":
      return checkLateResult(fold, e);
    case "effect_begin":
    case "effect_commit":
    case "effect_unknown":
    case "effect_resolved":
      return checkEffect(fold, e);
    case "approval_granted":
    case "approval_denied":
      return checkApproval(fold, e);
    case "cancelled":
      return checkCancelled(fold);
    case "context_edited":
      return checkContextEdit(fold, e);
    case "output_validated":
      return checkOutput(fold, e);
    case "mode_changed":
      return checkModeChanged(fold, e);
    case "agent_finished":
      return checkAgentFinished(fold, e);
    case "team_task_claimed":
    case "team_task_updated":
      return checkTask(fold, e);
    case "team_message":
    case "todos_updated":
    case "channel_delivery":
    case "schedule_fired":
      return checkUnique(fold, e);
    case "thread_started":
    case "injected":
    case "heartbeat":
    case "model_response":
    case "model_response_recovered":
    case "model_attempt_abandoned":
    case "permission_decision":
    case "hook_decision":
    case "approval_requested":
    case "snapshot":
    case "fork":
    case "parked":
    case "park_escalated":
    case "resumed":
    case "cancel_requested":
    case "stop_when_idle":
    case "turn_completed":
    case "schedule_skipped":
    case "log_repaired":
    case "retry_scheduled":
    case "budget_exceeded":
    case "compaction_failed":
    case "permission_rule_added":
    case "agent_spawned":
    case "handoff":
    case "team_task_created":
    case "context_preflight_blocked":
      return undefined;
    default:
      return assertNever(e);
  }
}

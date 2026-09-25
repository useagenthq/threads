import { assertNever } from "../assert-never";
import type { EventLine, Fold } from "../fold/state";
import type { KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import { checkAgentFinished, checkTask, checkUnique } from "./agents";
import {
  checkAnswerRejected,
  checkApproval,
  checkCancelled,
  checkContextEdit,
  checkEffect,
  checkLateResult,
  checkQuestionPark,
  checkToolCall,
  checkToolResult,
} from "./calls";
import { checkModeChanged, checkOutput, checkToolsChanged } from "./config";
import { checkNotYetPhase2 } from "./phase2";
import {
  checkCause,
  checkCompactionRequested,
  checkOutputStyle,
  checkRequestedCompacted,
} from "./requested";
import {
  checkAskClosed,
  checkMemberEnd,
  checkNotEnded,
  checkOperator,
  checkReceived,
  checkRefused,
  checkSent,
  checkStarted,
  checkTeamInput,
  checkTeamLog,
  checkTeamPark,
  checkWaits,
} from "./team";
import { checkToolsLoaded } from "./tools-loaded";
import {
  checkCompacted,
  checkInput,
  checkModelRequest,
  checkSettings,
} from "./turns";
import type { Violation } from "./violation";
import { checkWoken } from "./wake";

/**
 * The semantic rules that need earlier events (spec/schema/README.md, "Semantic rules" 6-13
 * and 17-49, but 43), checked against the fold before `line` is applied. Rules 1-4 are the chain's,
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
  const violation =
    checkNotYetPhase2(line.event) ??
    checkTeamLog(fold, line.event) ??
    check(fold, line.event);
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
      return checkToolsChanged(fold, e);
    case "tools_loaded":
      return checkToolsLoaded(fold, e);
    case "user_input":
      return checkTeamInput(fold, e) ?? checkInput(fold, e);
    case "steer":
      return checkInput(fold, e);
    case "model_request":
      return (
        checkModelRequest(fold) ??
        (e.data.purpose === "compaction"
          ? checkCause(fold, e.data.cause_event_id)
          : undefined)
      );
    case "settings_changed":
      return checkSettings(fold, e);
    case "compacted":
      return checkCompacted(fold, e) ?? checkRequestedCompacted(fold, e);
    case "compaction_failed":
      return checkCause(fold, e.data.cause_event_id);
    case "compaction_requested":
      return checkCompactionRequested(fold);
    case "injected":
      return checkOutputStyle(fold, e);
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
    case "schedule_skipped":
      return checkUnique(fold, e);
    case "thread_started":
    case "heartbeat":
    case "model_response":
    case "model_response_recovered":
    case "model_attempt_abandoned":
    case "permission_decision":
    case "hook_decision":
    case "approval_requested":
    case "snapshot":
    case "fork":
    case "park_escalated":
    case "resumed":
    case "cancel_requested":
    case "stop_when_idle":
    case "turn_completed":
    case "log_repaired":
    case "retry_scheduled":
    case "budget_exceeded":
    case "permission_rule_added":
    case "agent_spawned":
    case "handoff":
    case "team_task_created":
    case "context_preflight_blocked":
      return undefined;
    case "parked":
      return checkQuestionPark(fold, e) ?? checkTeamPark(fold, e);
    case "answer_rejected":
      return checkAnswerRejected(fold, e);
    case "woken":
      return checkNotEnded(fold) ?? checkWoken(fold, e);
    case "team_opened":
    case "monitor_set":
      return undefined;
    case "member_started":
      return checkStarted(fold, e);
    case "member_idle":
    case "member_ended":
      return checkMemberEnd(fold, e);
    case "wait_started":
    case "wait_finished":
    case "member_observed":
      return checkWaits(fold, e);
    case "message_sent":
      return checkSent(fold, e);
    case "message_received":
      return checkReceived(fold, e);
    case "mail_refused":
      return checkRefused(fold, e);
    case "ask_closed":
      return checkAskClosed(fold, e);
    case "operator_request":
    case "operator_refused":
    case "message_policy_decided":
      return checkOperator(fold, e);
    case "supervisor_decided":
      return undefined; // refused by checkNotYetPhase2 until the Phase 2 build
    default:
      return assertNever(e);
  }
}

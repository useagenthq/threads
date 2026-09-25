import type { z } from "zod";
import type { EventOf } from "../fold/state";
import type { KnownEvent, PermissionRule, Principal } from "../log";
import { err, ok, type Result } from "../result";
import type { EventDraft } from "../store";
import type { Chain } from "../verify";
import { type ControlError, type Plan, resumeIf } from "./control";
import { suggestedRules } from "./pending";

// approve and deny (spec/api.json Thread): an approver answers the open challenge once, before
// it expires, and may keep one of its suggested rules for the thread.

function requested(
  events: readonly KnownEvent[],
  challengeId: string,
): EventOf<"approval_requested"> | undefined {
  return events.findLast(
    (e): e is EventOf<"approval_requested"> =>
      e.type === "approval_requested" && e.data.challenge_id === challengeId,
  );
}

/** The challenge's request, if it is open and unexpired. */
function openChallenge(
  events: readonly KnownEvent[],
  chain: Chain,
  challengeId: string,
  now: number,
): Result<EventOf<"approval_requested">, ControlError> {
  const asked = requested(events, challengeId);
  const state = chain.fold.approvals.get(challengeId);
  if (asked === undefined || state === undefined)
    return err({ code: "not_found", message: `no challenge ${challengeId}` });
  if (state.consumed)
    return err({
      code: "approval_duplicate",
      message: `challenge ${challengeId} is already answered`,
    });
  return asked.data.expires_at <= now
    ? err({
        code: "approval_expired",
        message: `challenge ${challengeId} expired`,
      })
    : ok(asked);
}

/** approve and deny: the open challenge, answered once, before it expires. */
export function decide(
  challengeId: string,
  principal: Principal,
  now: number,
  answer:
    | {
        readonly grant: true;
        readonly rememberRule?: z.infer<typeof PermissionRule>;
      }
    | { readonly grant: false; readonly reason?: string },
): (events: readonly KnownEvent[], chain: Chain) => Result<Plan, ControlError> {
  return (events, chain) => {
    const open = openChallenge(events, chain, challengeId, now);
    if (!open.ok) return open;
    const asked = open.value;
    const rule = answer.grant ? answer.rememberRule : undefined;
    if (rule !== undefined && !suggested(events, asked.data.call_id, rule))
      return err({
        code: "invalid_request",
        message: "remember_rule must be one of the challenge's suggested_rules",
      });
    const binding = {
      challenge_id: challengeId,
      call_id: asked.data.call_id,
      args_hash: asked.data.args_hash,
    };
    const record = decision(answer, binding, principal);
    const resume = resumeIf(chain, { kind: "approval", id: challengeId });
    if (rule === undefined)
      return ok(resume === undefined ? { record } : { record, after: resume });
    const added = ruleAdded(rule, challengeId, principal);
    return ok({
      record,
      after: (id) => [added, ...(resume === undefined ? [] : resume(id))],
    });
  };
}

function decision(
  answer: { readonly grant: boolean; readonly reason?: string },
  binding: {
    readonly challenge_id: string;
    readonly call_id: string;
    readonly args_hash: string;
  },
  principal: Principal,
): EventDraft {
  return answer.grant
    ? {
        type: "approval_granted",
        type_version: 1,
        critical: true,
        actor: { kind: "approver", principal },
        data: binding,
      }
    : {
        type: "approval_denied",
        type_version: 1,
        critical: true,
        actor: { kind: "approver", principal },
        data: {
          ...binding,
          ...(answer.reason === undefined ? {} : { reason: answer.reason }),
        },
      };
}

/** The approver's "allow for this thread" rule. */
function ruleAdded(
  rule: string,
  challengeId: string,
  principal: Principal,
): EventDraft {
  return {
    type: "permission_rule_added",
    type_version: 1,
    critical: true,
    actor: { kind: "approver", principal },
    data: { rule, decision: "allow", challenge_id: challengeId },
  };
}

function suggested(
  events: readonly KnownEvent[],
  callId: string,
  rule: string,
): boolean {
  const call = events.find(
    (e) => e.type === "tool_call" && e.data.call_id === callId,
  );
  return (
    call?.type === "tool_call" &&
    suggestedRules(call.data.name, call.data.input).includes(rule)
  );
}

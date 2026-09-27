import type { EventOf } from "../fold/state";
import type { Facts } from "./cross";

// Rule 51's cross-log clause (spec/schema/README.md, "Semantic rules" 51): what only the host
// team's log and the member's own log together show. Checked by the `team` runner and every index
// rebuild, never by one log's validate_next.
// Reference: spec/tools/fixtures/ref_host_cross.py::supervised.

/**
 * Rule 51 across the logs: a decision's `ended` is its own member's `member_ended`, and its action
 * is `restart` exactly when the policy allows it and that end's result is `failed`. A position
 * among no log given here proves nothing, so it passes.
 */
export function supervised(
  e: EventOf<"supervisor_decided">,
  known: Facts,
): string | undefined {
  const { ended, member, policy, action, restarts_in_window: count } = e.data;
  const end = known.lines.get(`${ended.branch_id}/${ended.seq}`);
  if (end === undefined) return undefined;
  if (end.type !== "member_ended")
    return "a decision's ended names no member_ended";
  if (end.thread_id !== startedThread(member, known))
    return "a decision's ended is not its member's own member_ended";
  const failed = end.data.result.status === "failed";
  const allowed =
    policy.restart === "on_failure" && count < policy.max_restarts;
  return (action === "restart") === (allowed && failed)
    ? undefined
    : "restart exactly when allowed and failed";
}

/** The thread the `member_started` of this exact generation named. */
function startedThread(
  member: EventOf<"supervisor_decided">["data"]["member"],
  known: Facts,
): string | undefined {
  for (const started of known.hostStarts.values()) {
    const m = started.member;
    if (
      m.team === member.team &&
      m.name === member.name &&
      m.generation === member.generation
    )
      return started.thread_id;
  }
  return undefined;
}

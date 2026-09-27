import type { HostFold } from "../fold/host";
import { generationKey } from "../fold/host";
import type { EventOf, Fold } from "../fold/state";
import { invalid, type Violation } from "./violation";

// Semantic rule 51 on one host team's log (spec/schema/README.md, "Teams Phase 2"): each host
// member name's generations, and the one decision that ends each of them. The cross-log clause —
// a decision names its generation's own member_ended, and restarts exactly when the policy allows
// it and the member failed — is team/cross.ts's. Reference: spec/tools/fixtures/ref_host.py.

/**
 * Rule 51: a `member_started{restart_of: g}` is generation g + 1 of a name whose latest is g, and
 * follows either the `restart` decision on g (directly, with no provenance) or a `stop` decision
 * on g with an operator_request of this log as its root request.
 */
export function checkRestarted(
  fold: Fold,
  e: EventOf<"member_started">,
  restartOf: number,
): Violation {
  const { host } = fold.team;
  const { name, generation } = e.data.member;
  if (generation !== restartOf + 1 || host.generations.get(name) !== restartOf)
    return invalid("a restart starts the next generation of the latest one");
  const action = host.decided.get(generationKey(name, restartOf));
  if (action === "restart")
    return host.lastType === "supervisor_decided" &&
      e.data.provenance === undefined
      ? undefined
      : invalid("a supervised restart directly follows its decision");
  if (action === "stop" && e.data.provenance !== undefined)
    return operatorAsked(fold, e, e.data.provenance.root_request);
  return invalid("a restart names a generation the supervisor decided on");
}

/** An operator restart's root request is an `operator_request` of this same log. */
function operatorAsked(
  fold: Fold,
  e: EventOf<"member_started">,
  root: { readonly thread_id: string; readonly event_id: string },
): Violation {
  return root.thread_id === e.thread_id &&
    fold.team.requestEvents.has(root.event_id)
    ? undefined
    : invalid("an operator restart follows its operator_request");
}

/**
 * Rule 51: one decision per ended generation of a name this log started, carrying the count of
 * this log's earlier restarts inside the window, and restarting only where the policy allows.
 */
export function checkDecided(
  fold: Fold,
  e: EventOf<"supervisor_decided">,
): Violation {
  const { host } = fold.team;
  const { member, action, policy, restarts_in_window: logged } = e.data;
  const { name, generation } = member;
  if ((host.generations.get(name) ?? 0) < generation)
    return invalid("a decision on a generation this log never started");
  if (host.decided.has(generationKey(name, generation)))
    return invalid("a second decision on one ended generation");
  const count = restartsInWindow(host, name, e.time, policy.within_ms);
  if (logged !== count)
    return invalid("restarts_in_window is not the logged count");
  const allowed =
    policy.restart === "on_failure" && count < policy.max_restarts;
  return action === "restart" && !allowed
    ? invalid("a restart the policy does not allow")
    : undefined;
}

/** This log's earlier `restart` decisions for the name less than `window` before `now`. */
export function restartsInWindow(
  host: HostFold,
  name: string,
  now: number,
  window: number,
): number {
  const times = host.restarts.get(name) ?? [];
  return times.filter((at) => now - at < window).length;
}

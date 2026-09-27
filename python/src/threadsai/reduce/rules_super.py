"""Semantic rule 51 on one host team's log (spec/schema/README.md, "Teams Phase 2"): each host
member name's generations, and the one decision that ends each of them. The cross-log clause — a
decision names its generation's own member_ended, and restarts exactly when the policy allows it
and the member failed — is team/cross_host.py's. Reference: spec/tools/fixtures/ref_host.py."""

from pydantic.experimental.missing_sentinel import MISSING

from threadsai.log import MemberStartedEvent, SupervisorDecidedEvent
from threadsai.reduce.fold import Fold, Host


def generation_key(name: str, generation: int) -> str:
    """One host member generation, as a dict key (rule 51)."""
    return f"{name}/{generation}"


def restarted(fold: Fold, event: MemberStartedEvent, restart_of: int) -> str | None:
    """Rule 51: a `member_started{restart_of: g}` is generation g + 1 of a name whose latest is g,
    and follows either the `restart` decision on g (directly, with no provenance) or a `stop`
    decision on g with an operator_request of this log as its root request."""
    host, d = fold.host, event.data
    name, generation = d.member.name, d.member.generation
    if generation != restart_of + 1 or host.generations.get(name) != restart_of:
        return "51: a restart starts the next generation of the latest one"
    action = host.decided.get(generation_key(name, restart_of))
    if action == "restart":
        follows = host.last_type == "supervisor_decided" and d.provenance is MISSING
        return None if follows else "51: a supervised restart directly follows its decision"
    if action == "stop" and d.provenance is not MISSING:
        root = d.provenance.root_request
        mine = root.thread_id == event.thread_id and root.event_id in fold.team.request_events
        return None if mine else "51: an operator restart follows its operator_request"
    return "51: a restart names a generation the supervisor decided on"


def decided(fold: Fold, event: SupervisorDecidedEvent) -> str | None:
    """Rule 51: one decision per ended generation of a name this log started, carrying the count of
    this log's earlier restarts inside the window, and restarting only where the policy allows."""
    host, d = fold.host, event.data
    name, generation = d.member.name, d.member.generation
    if host.generations.get(name, 0) < generation:
        return "51: a decision on a generation this log never started"
    if generation_key(name, generation) in host.decided:
        return "51: a second decision on one ended generation"
    count = restarts_in_window(host, name, event.time, d.policy.within_ms)
    if d.restarts_in_window != count:
        return "51: restarts_in_window is not the logged count"
    allowed = d.policy.restart == "on_failure" and count < d.policy.max_restarts
    if d.action == "restart" and not allowed:
        return "51: a restart the policy does not allow"
    return None


def restarts_in_window(host: Host, name: str, now: int, window: int) -> int:
    """This log's earlier `restart` decisions for the name less than `window` before `now`."""
    return sum(1 for at in host.restarts.get(name, ()) if now - at < window)


def advance(host: Host, event: SupervisorDecidedEvent) -> None:
    """A decision's bookkeeping: its action, and a restart's time for the window count."""
    name, generation = event.data.member.name, event.data.member.generation
    host.decided[generation_key(name, generation)] = event.data.action
    if event.data.action == "restart":
        host.restarts.setdefault(name, []).append(event.time)

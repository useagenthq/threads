"""Background wakes (spec/schema/README.md, "Background wakes"; rules 32 and 45 for `woken`): a
wake opens a turn for background children's late results recorded in the same append, all of one
run that may still be woken (`wake_bar`), and acts as that run's principal. The run of each turn
and background spawn is the team fold's tracker (`TurnRun`); the writer reads `wake_bar` too."""

from collections.abc import Mapping, Sequence

from threads.log import (
    AgentFinishedEvent,
    Event,
    EventId,
    ParseError,
    Principal,
    ToolResultLateEvent,
    WokenEvent,
)
from threads.reduce.fold import Fold, PrincipalKey, TurnRun, Wake, reject
from threads.reduce.handlers import Handler, on


def _key(p: Principal) -> PrincipalKey:
    return (p.issuer, p.tenant, p.subject)


def _cause_calls(wake: Wake, causes: list[EventId]) -> list[str] | str:
    if len(set(causes)) != len(causes):
        return "woken names a cause twice"
    # The causes are the tail of the late-result block: a late result before the first cause
    # was recorded without a wake (an older writer, or a result held for another run).
    block = list(wake.trailing)
    if any(c not in wake.trailing for c in causes):
        return "a woken cause is not a late result of its own append"
    if len(block) - min(block.index(c) for c in causes) != len(causes):
        return "woken must name every late result of its own append, and only those"
    return [wake.trailing[c] for c in causes]


def wake_bar(fold: Fold, calls: Sequence[str]) -> str | None:
    """Why late results of these background calls must not wake the thread now, or None when
    they may (rule 32): no turn is open; the thread is not cancelled, has not handed off and has
    not ended; they were spawned by one run, which has not ended otherwise; and no thread or
    tree cancel request followed any of their spawns."""
    team = fold.team
    runs = [team.spawns.get(c) for c in calls]
    first = runs[0] if runs else None
    why: str | None = None
    if fold.in_turn:
        why = "woken while a turn is open"
    elif fold.cancelled or fold.handed_off or team.ended:
        why = "woken after the thread was cancelled, handed off or ended"
    elif first is None or any(r != first for r in runs):
        why = "woken causes are not the late results of one run's background children"
    elif first in team.ended_runs:
        why = "woken for a run that ended otherwise"
    elif any(c in team.barred for c in calls):
        why = "woken for a child a thread or tree cancel followed"
    return why


def spawn_run(fold: Fold, call: str) -> TurnRun | None:
    return fold.team.spawns.get(call)


def _woken(fold: Fold, event: WokenEvent) -> ParseError | None:
    if fold.in_turn:
        return reject(event, "woken while a turn is open")
    calls = _cause_calls(fold.wake, list(event.data.causes))
    if isinstance(calls, str):
        return reject(event, calls)
    why = wake_bar(fold, calls)
    if why is not None:
        return reject(event, why)
    run = spawn_run(fold, calls[0])
    if run is None or _key(event.actor.principal) != run.principal:
        return reject(event, "woken acts for another principal than the run that spawned it")
    return None


def advance(fold: Fold, event: Event) -> None:
    """The wake bookkeeping after an event passed validate_next. A woken opens a turn whose run
    the team fold has already set."""
    if isinstance(event, WokenEvent):
        fold.in_turn = True
    if isinstance(event, ToolResultLateEvent):
        fold.wake.trailing[event.event_id] = event.data.call_id
    elif not isinstance(event, AgentFinishedEvent):
        fold.wake.trailing.clear()


HANDLERS: Mapping[type, Handler] = dict([on(WokenEvent, _woken)])

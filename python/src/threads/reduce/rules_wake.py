"""Background wakes (spec/schema/README.md, "Background wakes"; rules 32 and 45 for `woken`): a
wake opens a turn for background children's late results recorded in the same append, all of one
run, and acts as that run's principal."""

from collections.abc import Mapping

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    Event,
    EventId,
    ParseError,
    Principal,
    ToolResultLateEvent,
    UserInputEvent,
    WokenEvent,
)
from threads.reduce.fold import Fold, PrincipalKey, Run, Wake, reject
from threads.reduce.handlers import Handler, on


def _key(p: Principal) -> PrincipalKey:
    return (p.issuer, p.tenant, p.subject)


def _causes_run(wake: Wake, causes: list[EventId]) -> Run | str:
    if len(set(causes)) != len(causes):
        return "woken names a cause twice"
    if len(causes) != len(wake.trailing):
        return "woken must name exactly the late results of its own append"
    runs = [wake.spawn_runs.get(wake.trailing[c]) if c in wake.trailing else None for c in causes]
    first = runs[0]
    if first is None or any(r is None for r in runs):
        return "a woken cause is not the late result of a background child in this append"
    return first if all(r == first for r in runs) else "woken names children of more than one run"


def _woken(fold: Fold, event: WokenEvent) -> ParseError | None:
    if fold.in_turn:
        return reject(event, "woken while a turn is open")
    if fold.cancelled:
        return reject(event, "woken after the thread was cancelled")
    run = _causes_run(fold.wake, list(event.data.causes))
    if isinstance(run, str):
        return reject(event, run)
    if _key(event.actor.principal) != run.principal:
        return reject(event, "woken acts for another principal than the run that spawned it")
    return None


def advance(fold: Fold, event: Event) -> None:
    """The wake bookkeeping after an event passed validate_next. A woken opens a turn whose run
    is the one that spawned the child it names."""
    wake = fold.wake
    if isinstance(event, WokenEvent):
        fold.in_turn = True
        wake.run = wake.spawn_runs.get(wake.trailing[event.data.causes[0]])
    elif isinstance(event, UserInputEvent):
        wake.run = Run(_key(event.actor.principal), event.event_id)
    elif isinstance(event, AgentSpawnedEvent) and event.data.mode == "background" and wake.run:
        wake.spawn_runs[event.data.call_id] = wake.run
    if isinstance(event, ToolResultLateEvent):
        wake.trailing[event.event_id] = event.data.call_id
    elif not isinstance(event, AgentFinishedEvent):
        wake.trailing.clear()


HANDLERS: Mapping[type, Handler] = dict([on(WokenEvent, _woken)])

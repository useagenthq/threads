"""Where the open (or last) turn of a log began: its user_input, its woken, or the received mail
that opened it (spec/schema/README.md, "Which events open a turn"), read from the events alone
with the fold's own rules (team_fold.mail_renders)."""

from collections.abc import Sequence

from threads.log import (
    Event,
    MessageReceivedEvent,
    ParkAddress,
    ParkedEvent,
    ResumedEvent,
    TeamOpenedEvent,
    TurnCompletedEvent,
    UserInputEvent,
    WaitStartedEvent,
    WokenEvent,
)
from threads.reduce.team_fold import mail_renders, monitor_id


def _opens(event: Event, parks: Sequence[ParkAddress], settle: set[str]) -> bool:
    if isinstance(event, UserInputEvent | WokenEvent):
        return True
    if not isinstance(event, MessageReceivedEvent):
        return False
    env = event.data.envelope
    resolved = all(p.kind == "member" and p.id == env.monitor_id for p in parks)
    return mail_renders(env, settle) and resolved


def turn_start(events: Sequence[Event]) -> int | None:
    """The index of the event that opened the open (or last) turn; None before any turn. A team
    log opens none."""
    if events and isinstance(events[0], TeamOpenedEvent):
        return None
    in_turn, start = False, None
    parks: list[ParkAddress] = []
    settle: set[str] = set()
    for i, event in enumerate(events):
        if not in_turn and _opens(event, parks, settle):
            in_turn, start = True, i
        if isinstance(event, TurnCompletedEvent):
            in_turn = False
        elif isinstance(event, ParkedEvent):
            parks.append(event.data.address)
        elif isinstance(event, ResumedEvent) and event.data.address in parks:
            parks.remove(event.data.address)
        elif isinstance(event, WaitStartedEvent):
            settle.update(monitor_id(event, m.name) for m in event.data.members)
    return start

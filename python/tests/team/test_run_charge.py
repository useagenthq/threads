"""Which run a turn is charged to (spec/schema/README.md, "Teams"): a lead's turn woken by a
member's settlement belongs to the run whose request started that member, even after a later run
began (21D review M3), not to the latest input's run. Mirrors TypeScript's
test/team/run-charge.test.ts."""

from team.team_kit import CASES, case_logs, verified

from threads.log import (
    Budget,
    Event,
    EventId,
    MessageReceivedEvent,
    ThreadId,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.loop.budget import own
from threads.reduce.openers import run_opener
from threads.result import Ok

_READ = verified(case_logs("team-settle-wakes-lead")["lead"])
assert isinstance(_READ, Ok)
LEAD = list(_READ.value.fold.events)
THREAD = ThreadId("0192a000-0000-7000-8000-0000000000b1")


def _input(event_id: str | None, most: int) -> Event:
    """The recorded user_input, with a run budget and, for a later run, a new id."""
    first = LEAD[1]
    assert isinstance(first, UserInputEvent)
    data = first.data.model_copy(update={"budget": Budget(max_model_requests=most)})
    ident = first.event_id if event_id is None else EventId(event_id)
    return first.model_copy(update={"data": data, "event_id": ident})


def test_a_leads_wake_turn_is_charged_to_the_run_that_started_the_member() -> None:
    started, rest = LEAD[0], LEAD[2:]
    settled = next(i for i, e in enumerate(rest) if isinstance(e, MessageReceivedEvent))
    ended = next(e for e in reversed(rest[:settled]) if isinstance(e, TurnCompletedEvent))
    later = "0192e001-0000-7000-8000-0000000000f1"
    events = [
        started,
        _input(None, 20),
        *rest[:settled],
        _input(later, 30),
        ended,
        *rest[settled:],
    ]
    runs = [c.budget_id for c in own(THREAD, events) if c.scope == "run"]
    assert runs == [f"run:{THREAD}:{LEAD[1].event_id}"]


def test_a_woken_turn_belongs_to_the_run_of_the_turn_that_spawned_its_child() -> None:
    read = verified((CASES / "woken-principal-mail-run" / "log.jsonl").read_bytes())
    assert isinstance(read, Ok)
    opener = run_opener(read.value.fold.events)
    assert isinstance(opener, MessageReceivedEvent)
    assert opener.seq == 6  # noqa: PLR2004 - the mail that opened the spawning turn

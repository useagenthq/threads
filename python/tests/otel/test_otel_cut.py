"""A turn that ends with a call still open closes it as cut (spec/otel/README.md, "Closing a
turn closes its children"). No known path on main writes such a log, so it is synthetic: the
walk is fed events directly, past validation."""

from otel_goldens_kit import goldens

from threads.log import Event, UnknownEvent, UserInputEvent
from threads.otel.walk import WalkInput, walk


def test_a_call_open_at_turn_end_is_cut() -> None:
    golden = next(g for g in goldens() if g.case.name == "otel-turn-model-tool")
    branch = golden.case.branches[0]
    events: list[Event] = [
        e
        for s in golden.logs[branch].segments
        for e, _ in s.events
        if not isinstance(e, UnknownEvent)
    ]
    # thread_started .. the tool_call and its permission (seq 1-6), then the turn's end at seq 7.
    end = events[-1].model_copy(update={"seq": 7})
    opener = next(e for e in events if isinstance(e, UserInputEvent))
    walked = walk(
        WalkInput("local", branch, [*events[:6], end], {opener.event_id}, False, lambda _: None)
    )
    tool = next(s for s in walked.spans if s.name == "execute_tool read_file")
    assert tool.status == "cut"
    assert tool.attributes["threads.cut"] is True
    assert tool.close_seq == 7  # noqa: PLR2004 - counted spans
    turn = next(s for s in walked.spans if s.name.startswith("invoke_agent"))
    assert turn.status is None
    assert "threads.cut" not in turn.attributes

"""The ask_user question a run has open, derived from the events alone. A frame must show the
question that was open where it was read, not the one open now, so this reads a prefix of the log
rather than the fold's current view. It only renders a question; the rules for matching an answer to
one stay in threadsai.log.ask_user, where Thread.answer applies them."""

from collections.abc import Sequence
from dataclasses import dataclass

from threadsai.log import CallId, Event, ParkedEvent, ResumedEvent, ToolCallEvent
from threadsai.log.ask_user import Ask, ask_of, question_text


@dataclass(frozen=True, slots=True)
class OpenAsk:
    call_id: CallId
    ask: Ask


def open_ask(events: Sequence[Event]) -> OpenAsk | None:
    """The question open at the end of `events`: the oldest input park no resumed has closed, with
    the ask its tool_call recorded. None when nothing waits on an answer."""
    waiting: list[str] = []
    for event in events:
        if isinstance(event, ParkedEvent) and event.data.address.kind == "input":
            waiting.append(event.data.address.id)
        elif (
            isinstance(event, ResumedEvent)
            and event.data.address.kind == "input"
            and event.data.address.id in waiting
        ):
            waiting.remove(event.data.address.id)
    if not waiting:
        return None
    call_id = waiting[0]
    call = next(
        (e for e in events if isinstance(e, ToolCallEvent) and e.data.call_id == call_id), None
    )
    if call is None:
        return None
    ask = ask_of(dict(call.data.input))
    return None if ask is None else OpenAsk(CallId(call_id), ask)


def ask_text(ask: Ask) -> str:
    """The question and its choices as the framework words them: an INPUT_REQUIRED task's status
    message. One wording for a channel, a browser and a partner."""
    return question_text(ask)

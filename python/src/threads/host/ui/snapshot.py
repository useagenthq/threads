"""An AG-UI replay's opening (spec/schema/ui/README.md, "Replays and the snapshot"):
MESSAGES_SNAPSHOT of the thread's history through the snapshot point, then the open-state
preamble. The stock client merges a snapshot by id, so it holds the whole history."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING

from threads.host.ui.ag_ui import MODEL_STEP, ag_ui_events, started
from threads.host.ui.ag_ui_fold import UserTurn, fold_ag_ui
from threads.host.ui.facts import RunFacts
from threads.host.ui.frame import Chunk
from threads.log import Event, UserInputEvent
from threads.store.receipts import input_text

if TYPE_CHECKING:
    from pydantic import JsonValue


def user_message_id(e: UserInputEvent, receipts: Mapping[str, str]) -> str:
    """The client's own id for a user message: the one it sent, else a ui receipt's (runs
    recorded before client_message_id), else the input's event id."""
    if e.data.client_message_id is not MISSING:
        return e.data.client_message_id
    return receipts.get(e.event_id, e.event_id)


@dataclass
class _Turn:
    id: str
    text: str
    frames: list[Chunk] = field(default_factory=list[Chunk])
    facts: RunFacts = field(default_factory=RunFacts)


def messages_snapshot(history: Sequence[Event], receipts: Mapping[str, str]) -> Chunk:
    """MESSAGES_SNAPSHOT of `history`: the resolved chain from its start through the point."""
    turns: list[_Turn] = []
    for e in history:
        if isinstance(e, UserInputEvent):
            turns.append(_Turn(user_message_id(e, receipts), input_text(e)))
            continue
        if not turns:
            continue
        turn = turns[-1]
        turn.frames.extend(ag_ui_events(e, turn.facts))
        turn.facts.add(e)
    messages: list[JsonValue] = [*fold_ag_ui([UserTurn(t.id, t.text, t.frames) for t in turns])]
    return {"type": "MESSAGES_SNAPSHOT", "messages": messages}


def preamble(facts: RunFacts) -> list[Chunk]:
    """What the log shows open at the snapshot point: a model step, running legacy subagents."""
    step = [] if facts.open_step() is None else [MODEL_STEP]
    return [*step, *(started(s) for s in facts.running())]

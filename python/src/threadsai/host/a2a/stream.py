"""A task's SSE frames: first a task snapshot, then a statusUpdate per state change, then an
artifactUpdate for the final output. Every frame is a function of the committed events alone —
there are no live token deltas here — so a resume at any frame boundary replays exactly the frames
after it, and a reconnecting client never sees one twice."""

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threadsai._generated.a2a_v1 import (
    StreamResponse,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)
from threadsai.a2a.protocol import A2aFault, SseFrame, fault, is_settled
from threadsai.host.a2a.read import Located, slices_of
from threadsai.host.a2a.state import Slice, task_of
from threadsai.host.runs import Runner
from threadsai.log import Principal

POLL_S: Final = 0.05
_CURSOR: Final = re.compile(r"^(\d+):(\d+)$")


@dataclass(frozen=True, slots=True, order=True)
class Cursor:
    """A frame's position: the seq it was read at, and which frame of that seq it is."""

    seq: int
    k: int


def parse_cursor(raw: str | None) -> Cursor | A2aFault | None:
    """`<seq>:<k>`, or the InvalidParamsError a malformed Last-Event-ID earns."""
    if raw is None or raw == "":
        return None
    found = _CURSOR.match(raw)
    if found is None:
        return fault("InvalidParamsError", f"Last-Event-ID {raw} is not <seq>:<k>")
    return Cursor(int(found.group(1)), int(found.group(2)))


@dataclass(frozen=True, slots=True)
class Frame:
    at: Cursor
    item: StreamResponse


def frames_of(slices: Sequence[Slice]) -> tuple[Frame, ...]:
    """The frames the run's states have produced, in order: a task snapshot, then one statusUpdate
    per state change, then the artifactUpdate of a completed run. Computed from the prefix slices
    alone, so the list for a given log is always the same, and only ever grows at its end."""
    frames: list[Frame] = []
    shown: str | None = None
    for view in slices:
        task = task_of(view)
        seq = view.own[-1].seq if view.own else 0
        if shown is None:
            frames.append(Frame(Cursor(seq, 0), StreamResponse(task=task)))
            shown = task.status.state
            continue
        if task.status.state == shown:
            continue
        shown = task.status.state
        frames.append(
            Frame(
                Cursor(seq, 0),
                StreamResponse(
                    statusUpdate=TaskStatusUpdateEvent(
                        taskId=task.id, contextId=view.context_id, status=task.status
                    )
                ),
            )
        )
        for k, artifact in enumerate(() if task.artifacts is MISSING else task.artifacts, start=1):
            frames.append(
                Frame(
                    Cursor(seq, k),
                    StreamResponse(
                        artifactUpdate=TaskArtifactUpdateEvent(
                            taskId=task.id,
                            contextId=view.context_id,
                            artifact=artifact,
                            lastChunk=True,
                        )
                    ),
                )
            )
    return tuple(frames)


async def follow(
    runner: Runner,
    principal: Principal,
    at: Located,
    cursor: Cursor | None,
    item: Callable[[StreamResponse], str],
) -> AsyncIterator[SseFrame]:
    """The frames after `cursor`, following the log until the task settles. Only committed events
    are streamed, so a follower that joins late and one that was there from the start see the same
    frames in the same order."""
    seen = cursor
    while True:
        slices = await slices_of(runner, principal.tenant, at)
        if not slices:
            return
        for frame in frames_of(slices):
            if seen is None or frame.at > seen:
                seen = frame.at
                yield SseFrame(item(frame.item), f"{frame.at.seq}:{frame.at.k}")
        if is_settled(task_of(slices[-1]).status.state):
            return
        await asyncio.sleep(POLL_S)


async def one(data: str) -> AsyncIterator[SseFrame]:
    """A stream of one frame: a task refused before it existed has no log to follow."""
    yield SseFrame(data)

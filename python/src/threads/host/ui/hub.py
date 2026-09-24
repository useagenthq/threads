"""The process's live hub (spec/schema/ui/README.md, "Live text"): the runs this process executes
publish their redacted text deltas here, keyed by tenant and thread, and the UI connections on that
thread listen. Per (request, part) it remembers that a delta went out, until the request commits or
is abandoned, so a connection registering mid-part never takes a later delta for the first."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from threads.host.ui.framed import part_id
from threads.host.ui.live import Delta
from threads.log import (
    ModelAttemptAbandonedEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
)

type Listener = Callable[[Delta | None], None]
"""Told each delta, or None when the thread's log may have changed."""


@dataclass
class _Channel:
    started: set[str] = field(default_factory=set[str])
    listeners: list[Listener] = field(default_factory=list[Listener])


@dataclass(frozen=True, slots=True)
class Registration:
    missed: frozenset[str]
    """Parts a delta had already gone out for: this connection never streams them live."""
    stop: Callable[[], None]


class LiveHub:
    def __init__(self) -> None:
        self._threads: dict[str, _Channel] = {}

    def _drop(self, key: str, c: _Channel) -> None:
        if not c.started and not c.listeners:
            self._threads.pop(key, None)

    def delta(self, tenant: str, thread: str, delta: Delta) -> None:
        """A redacted delta of a run on `thread`."""
        key = _key(tenant, thread)
        c = self._threads.setdefault(key, _Channel())
        c.started.add(part_id(delta.request_id, delta.part))
        for listen in list(c.listeners):
            listen(delta)

    def appended(self, tenant: str, thread: str, e: object) -> None:
        """An event a run of this process appended on `thread`: listeners re-read the log."""
        key = _key(tenant, thread)
        c = self._threads.get(key)
        if c is None:
            return
        if isinstance(
            e, ModelResponseEvent | ModelResponseRecoveredEvent | ModelAttemptAbandonedEvent
        ):
            prefix = f"{e.data.request_event_id}:"
            c.started -= {p for p in c.started if p.startswith(prefix)}
        for listen in list(c.listeners):
            listen(None)
        self._drop(key, c)

    def listen(self, tenant: str, thread: str, listener: Listener) -> Registration:
        """Registers before the connection reads the head, so no delta falls between the two."""
        key = _key(tenant, thread)
        c = self._threads.setdefault(key, _Channel())
        c.listeners.append(listener)

        def stop() -> None:
            if listener in c.listeners:
                c.listeners.remove(listener)
            self._drop(key, c)

        return Registration(frozenset(c.started), stop)


def _key(tenant: str, thread: str) -> str:
    """The tenant is part of the key, so a thread id alone never reaches another tenant's text."""
    return json.dumps([tenant, thread])

"""The telemetry protocol (spec/api.json `Exporter`): what `host(telemetry=...)` calls.
`threads.otel.otel()` implements it; core only knows the shape and which store a host bound it
to."""

from dataclasses import dataclass
from typing import Literal, Protocol
from weakref import WeakKeyDictionary

from threads.agents.store import Store
from threads.log import BranchId, ThreadId
from threads.result import Err, Ok


@dataclass(frozen=True, slots=True)
class SkippedBranch:
    """A branch `Exporter.sync()` could not read, and the head it failed at."""

    branch_id: BranchId
    thread_id: ThreadId
    code: str
    """Why it didn't read: a log error code such as log_corrupt."""
    head_seq: int


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one `Exporter.sync()` sent."""

    spans: int
    possibly_lost_events: int
    skipped: tuple[SkippedBranch, ...]


@dataclass(frozen=True, slots=True)
class SyncError:
    code: Literal["collector_unavailable", "collector_rejected"]
    message: str
    status: int | None = None
    """The collector's HTTP status, when it answered."""


class Exporter(Protocol):
    """A telemetry exporter, such as `otel()`."""

    async def sync(self) -> Ok[SyncReport] | Err[SyncError]:
        """Sends every span that closed since the last acknowledged sync."""
        ...


_BOUND: "WeakKeyDictionary[Exporter, Store]" = WeakKeyDictionary()


def bind_telemetry(exporter: Exporter, store: Store) -> None:
    """`host(store=..., telemetry=...)`: an exporter made without a store exports the host's."""
    _BOUND[exporter] = store


def bound_store(exporter: Exporter) -> Store | None:
    """The store a host bound the exporter to, if any."""
    return _BOUND.get(exporter)

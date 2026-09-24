"""Schedules (spec/api.json `Schedule`): a cron expression in an IANA zone starts
a run of a host agent at each occurrence, on the schedule's thread (one while its agent's config
is unchanged: a config change starts a new thread).

A due occurrence (tenant, schedule id, scheduled instant UTC) is first reserved as a pending row,
with the agent, input and timezone it fires with frozen, and then decided under its thread's
writer (`host.occurrences`). Every pass decides all of the tenant's pending rows, whatever
schedules are configured now, so a reservation outlives a restart or its schedule's removal.
Operator config: the local tenant, and the schedule itself as the principal.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from pydantic import TypeAdapter, ValidationError

from threads._generated.events_v1 import Name
from threads._generated.host_api_v1 import Input
from threads.agents.config import ConfigError
from threads.agents.store import now_ms, open_store
from threads.host.cron import MINUTE_MS, is_due, parse_cron, zone
from threads.host.occurrences import decide_thread
from threads.host.runs import Runner
from threads.host.schedule_pass import Pass, isolated
from threads.host.schedule_threads import reserve_due
from threads.log import ThreadId
from threads.result import Ok
from threads.store import LOCAL_TENANT
from threads.store.schedules import Due

_NAME: TypeAdapter[Name] = TypeAdapter(Name)


@dataclass(frozen=True, slots=True)
class Schedule:
    """spec/api.json `Schedule`."""

    id: str
    agent: str
    """A key of host(agents=...)."""
    cron: str
    input: Input
    timezone: str = "UTC"
    """IANA name."""


def _check_id(schedule_id: str) -> None:
    """spec/api.json Schedule.id is a Name."""
    try:
        _NAME.validate_python(schedule_id)
    except ValidationError:
        why = "an id is lowercase letters, digits and underscores, starting with a letter, up to 64"
        raise ConfigError("invalid_config", f"schedule {schedule_id}: {why}") from None


class Scheduler:
    def __init__(
        self,
        runner: Runner,
        schedules: Sequence[Schedule],
        clock: Callable[[], int] = now_ms,
        *,
        tenant: str = LOCAL_TENANT,
    ) -> None:
        self._runner = runner
        self._schedules = schedules
        self._clock = clock
        self._tenant = tenant

    def check(self, agents: Callable[[str], object]) -> None:
        """ready(): every schedule names a host agent, a valid cron and a known zone."""
        for s in self._schedules:
            _check_id(s.id)
            if agents(s.agent) is None:
                raise ConfigError("invalid_config", f"schedule {s.id}: no agent {s.agent}")
            parse_cron(s.cron)
            zone(s.timezone)

    async def run(self) -> None:
        """Checks each minute boundary as it passes; runs until cancelled. A failed pass is
        reported and the next minute's pass runs anyway, as the TS host does with a failed tick."""
        started_at = self._clock()
        while True:
            await isolated("schedule tick", lambda: self.tick(started_at, self._clock()))
            now = self._clock()
            await asyncio.sleep((now - now % MINUTE_MS + MINUTE_MS - now) / 1000)

    async def tick(self, started_at: int, now: int) -> None:
        """One pass at `now`; `started_at` is when this host became ready. Each schedule's
        reservation and each thread's decisions fail alone, and recovery always runs."""
        p = Pass(self._runner, self._runner.store(self._tenant), self._tenant)
        for schedule in self._schedules:
            await isolated(
                f"schedule {schedule.id}", partial(self._reserve, p, schedule, started_at, now)
            )
        # Reserved first, so a thread's backlog and its newly due occurrences are decided in one
        # writer hold, in occurrence order.
        await isolated("the pending sweep", partial(self._sweep, p))
        await self._resume_open(p)

    async def _sweep(self, p: Pass) -> None:
        """Decides every pending row of the tenant, thread by thread in occurrence order."""
        rows = (await open_store(p.store)).tables.schedules
        threads = dict.fromkeys(r.thread_id for r in await rows.pending())
        for thread in threads:
            await isolated(f"schedule thread {thread}", partial(decide_thread, p, thread))

    async def _reserve(self, p: Pass, schedule: Schedule, started_at: int, now: int) -> None:
        """Reserves the schedule's occurrences due since its last one (or since ready)."""
        rows = (await open_store(p.store)).tables.schedules
        after = await rows.last(schedule.id) or started_at
        cron = parse_cron(schedule.cron)
        first = after - after % MINUTE_MS + MINUTE_MS
        # ponytail: one is_due per minute since the last occurrence; step by cron fields if a
        # host is ever down for months.
        due = [at for at in range(first, now + 1, MINUTE_MS) if is_due(cron, schedule.timezone, at)]
        if not due:
            return
        found = [
            Due(
                schedule.id, at, schedule.agent, schedule.input, schedule.timezone, at <= started_at
            )
            for at in due
        ]
        await reserve_due(p, await p.started(schedule.agent), found, self._clock())

    async def _resume_open(self, p: Pass) -> None:
        """A run whose input is durable but that never went (its host died, or the writer was
        held when it fired) runs on from the log. Each thread is looked at alone."""
        sq = await open_store(p.store)
        # ponytail: reads every schedule thread's log each tick, old ones included; index threads
        # with an open turn if schedules pile up threads.
        for thread in await sq.tables.schedules.threads():
            await isolated(f"recovering {thread}", partial(self._resume, p, thread))

    async def _resume(self, p: Pass, thread: ThreadId) -> None:
        sq = await open_store(p.store)
        root = await sq.root(thread)
        read = await sq.read(root.value, self._clock()) if isinstance(root, Ok) else None
        if not isinstance(root, Ok) or not isinstance(read, Ok):
            return
        fold = read.value.fold
        if fold.in_turn and not fold.parked and not self._runner.running(root.value):
            await self._runner.resume(p.store, thread, root.value)

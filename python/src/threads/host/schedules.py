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
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import TypeAdapter, ValidationError

from threads._generated.events_v1 import Name
from threads._generated.host_api_v1 import Input
from threads.agents.config import ConfigError
from threads.agents.run import pinned_start
from threads.agents.store import now_ms, open_store
from threads.host.occurrences import Pass, decide_thread
from threads.host.runs import Runner
from threads.result import Ok
from threads.store import LOCAL_TENANT
from threads.store.schedules import Due

MINUTE_MS: Final = 60_000
_NAME: TypeAdapter[Name] = TypeAdapter(Name)
_RANGES: Final = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
"""minute, hour, day of month, month, day of week (0 is Sunday; 7 is accepted as Sunday)."""


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


@dataclass(frozen=True, slots=True)
class Cron:
    fields: tuple[frozenset[int], ...]
    dom_any: bool
    dow_any: bool

    def matches(self, at: datetime) -> bool:
        minute, hour, dom, month, dow = self.fields
        weekday = (at.weekday() + 1) % 7
        day = (at.day in dom, weekday in dow)
        # Vixie cron: with both day fields restricted, either one matching is enough.
        day_ok = all(day) if self.dom_any or self.dow_any else any(day)
        return at.minute in minute and at.hour in hour and at.month in month and day_ok


def parse_cron(expression: str) -> Cron:
    """Five fields of numbers, `*`, lists, ranges and steps. Raises ConfigError otherwise."""
    parts = expression.split()
    if len(parts) != len(_RANGES):
        raise ConfigError("invalid_config", f"cron {expression!r} needs five fields")
    fields = tuple(
        _field(p, lo, hi, expression) for p, (lo, hi) in zip(parts, _RANGES, strict=True)
    )
    dow = frozenset(0 if d == 7 else d for d in fields[4])  # noqa: PLR2004 - 7 is Sunday too
    return Cron((*fields[:4], dow), parts[2] == "*", parts[4] == "*")


def _field(text: str, lo: int, hi: int, expression: str) -> frozenset[int]:
    values: set[int] = set()
    top = 7 if hi == 6 else hi  # noqa: PLR2004 - day of week accepts 7
    for item in text.split(","):
        base, _, step_text = item.partition("/")
        try:
            step = int(step_text) if step_text else 1
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                first, last = base.split("-", 1)
                start, end = int(first), int(last)
            else:
                start = int(base)
                end = hi if step_text else start
        except ValueError:
            raise ConfigError(
                "invalid_config", f"cron {expression!r}: bad field {text!r}"
            ) from None
        if not (lo <= start <= end <= top) or step < 1:
            raise ConfigError("invalid_config", f"cron {expression!r}: {text!r} out of range")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def _check_id(schedule_id: str) -> None:
    """spec/api.json Schedule.id is a Name."""
    try:
        _NAME.validate_python(schedule_id)
    except ValidationError:
        why = "an id is lowercase letters, digits and underscores, starting with a letter, up to 64"
        raise ConfigError("invalid_config", f"schedule {schedule_id}: {why}") from None


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("invalid_config", f"unknown timezone {name!r}") from None


def is_due(cron: Cron, timezone: str, at: int) -> bool:
    """Whether the instant `at` (UTC ms, on a minute) fires, by the wall-clock rules: a local time
    skipped by a spring-forward runs at the first valid instant after it; a local time repeated by
    a fall-back runs only at its first instance (fold 0)."""
    tz = zone(timezone)
    here = datetime.fromtimestamp(at / 1000, UTC).astimezone(tz)
    before = datetime.fromtimestamp((at - MINUTE_MS) / 1000, UTC).astimezone(tz)
    wall, skipped = here.replace(tzinfo=None, fold=0), before.replace(tzinfo=None, fold=0)
    step = timedelta(minutes=1)
    skipped += step
    while skipped < wall:  # wall minutes that never happened: they run now
        if cron.matches(skipped):
            return True
        skipped += step
    return here.fold == 0 and cron.matches(wall)


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
        self._pass = Pass(runner, runner.store(tenant), tenant)

    def check(self, agents: Callable[[str], object]) -> None:
        """ready(): every schedule names a host agent, a valid cron and a known zone."""
        for s in self._schedules:
            _check_id(s.id)
            if agents(s.agent) is None:
                raise ConfigError("invalid_config", f"schedule {s.id}: no agent {s.agent}")
            parse_cron(s.cron)
            zone(s.timezone)

    async def run(self) -> None:
        """Checks each minute boundary as it passes; runs until cancelled."""
        started_at = self._clock()
        while True:
            try:
                await self.tick(started_at, self._clock())
            except ConfigError as error:
                # An agent that can't be set up now (a secret, a server) is retried next minute:
                # nothing was stored for it. Reported as the TS host reports a failed tick.
                sys.stderr.write(f"threads host: schedule tick failed: {error.message}\n")
            now = self._clock()
            await asyncio.sleep((now - now % MINUTE_MS + MINUTE_MS - now) / 1000)

    async def tick(self, started_at: int, now: int) -> None:
        """One pass at `now`; `started_at` is when this host became ready."""
        for schedule in self._schedules:
            await self._reserve_due(schedule, started_at, now)
        # Reserved first, so a thread's backlog and its newly due occurrences are decided in one
        # writer hold, in occurrence order.
        await self._sweep()
        await self._resume_open()

    async def _sweep(self) -> None:
        """Decides every pending row of the tenant, thread by thread in occurrence order."""
        rows = (await open_store(self._pass.store)).tables.schedules
        threads = dict.fromkeys(r.thread_id for r in await rows.pending())
        for thread in threads:
            await decide_thread(self._pass, thread)

    async def _reserve_due(self, schedule: Schedule, started_at: int, now: int) -> None:
        """Reserves the schedule's occurrences due since its last one (or since ready)."""
        sq = await open_store(self._pass.store)
        rows = sq.tables.schedules
        after = await rows.last(schedule.id) or started_at
        cron = parse_cron(schedule.cron)
        first = after - after % MINUTE_MS + MINUTE_MS
        # ponytail: one is_due per minute since the last occurrence; step by cron fields if a
        # host is ever down for months.
        due = [at for at in range(first, now + 1, MINUTE_MS) if is_due(cron, schedule.timezone, at)]
        if not due:
            return
        definition = self._runner.bound_to(schedule.agent).definition
        started = await pinned_start(definition, self._pass.store)
        found = [
            Due(
                schedule.id, at, schedule.agent, schedule.input, schedule.timezone, at <= started_at
            )
            for at in due
        ]
        await rows.reserve_due(started, found, self._clock())

    async def _resume_open(self) -> None:
        """A run whose input is durable but that never went (its host died, or the writer was
        held when it fired) runs on from the log."""
        sq = await open_store(self._pass.store)
        for thread in await sq.tables.schedules.threads():
            root = await sq.root(thread)
            read = await sq.read(root.value, self._clock()) if isinstance(root, Ok) else None
            if not isinstance(root, Ok) or not isinstance(read, Ok):
                continue
            fold = read.value.fold
            if fold.in_turn and not fold.parked and not self._runner.running(root.value):
                await self._runner.resume(self._pass.store, thread, root.value)

"""Schedules (spec/api.json `Schedule`, ): a cron expression in an IANA zone starts
a run of a host agent at each occurrence.

An occurrence is claimed in `schedule_occurrences` before its schedule_fired is appended, so two
hosts that see the same due minute start one run. Each occurrence runs on a new thread, so an
occurrence never overlaps the last one's run. Operator config: the local tenant, and the
schedule itself as the principal.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from threads._generated.host_api_v1 import Input
from threads.agents.config import ConfigError
from threads.agents.intake import Intake
from threads.agents.store import now_ms, open_store
from threads.host.runs import Runner
from threads.log import BranchId, Principal, ThreadId
from threads.store import LOCAL_TENANT, Draft, StoredEvent
from threads.store.lines import uuid7
from threads.thread.handle import Thread

MINUTE_MS: Final = 60_000
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
        self, runner: Runner, schedules: Sequence[Schedule], clock: Callable[[], int] = now_ms
    ) -> None:
        self._runner = runner
        self._schedules = schedules
        self._clock = clock

    def check(self, agents: Callable[[str], object]) -> None:
        """ready(): every schedule names a host agent, a valid cron and a known zone."""
        for s in self._schedules:
            if agents(s.agent) is None:
                raise ConfigError("invalid_config", f"schedule {s.id}: no agent {s.agent}")
            parse_cron(s.cron)
            zone(s.timezone)

    async def run(self) -> None:
        """Checks each minute boundary as it passes; runs until cancelled."""
        while True:
            now = self._clock()
            at = now - now % MINUTE_MS
            await self.tick(at)
            await asyncio.sleep((at + MINUTE_MS - self._clock()) / 1000)

    async def tick(self, at: int) -> None:
        """Fires every schedule due at the minute starting at `at` (UTC ms)."""
        for schedule in self._schedules:
            if is_due(parse_cron(schedule.cron), schedule.timezone, at):
                await self.fire(schedule, at)

    async def fire(self, schedule: Schedule, at: int) -> bool:
        """Claims the occurrence and starts its run; False when another scheduler had it."""
        store = self._runner.store(LOCAL_TENANT)
        sq = await open_store(store)
        now = self._clock()
        thread_id, branch_id = ThreadId(uuid7(now)), BranchId(uuid7(now))
        if not await sq.tables.claim(schedule.id, at, thread_id, now):
            return False
        await sq.create(thread_id, branch_id, now)
        fired = uuid7(now)
        data = {
            "schedule_id": schedule.id,
            "occurrence_id": f"{schedule.id}:{at}",
            "scheduled_for": at,
            "timezone": schedule.timezone,
        }
        recorded: asyncio.Future[StoredEvent] = asyncio.get_running_loop().create_future()
        intake = Intake(
            "schedule",
            recorded,
            before=(Draft("schedule_fired", data, {"kind": "scheduler"}, True, fired),),
            delivery_event_id=fired,
        )
        bound = self._runner.bound_to(schedule.agent)
        thread = Thread(thread_id, branch_id, store)
        who = Principal(issuer="schedule", tenant=LOCAL_TENANT, subject=schedule.id)
        task = self._runner.launch(bound, schedule.input, thread, who, intake=intake)
        # Returns once the occurrence's input is durable (or its run ended without one).
        await asyncio.wait({recorded, task}, return_when=asyncio.FIRST_COMPLETED)
        return True

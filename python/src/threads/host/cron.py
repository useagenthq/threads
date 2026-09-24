"""Cron expressions (spec/api.json `Schedule.cron`) read in an IANA zone: which UTC minutes an
expression fires at, by the wall-clock rules across daylight-saving changes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from threads.agents.config import ConfigError

MINUTE_MS: Final = 60_000
_RANGES: Final = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
"""minute, hour, day of month, month, day of week (0 is Sunday; 7 is accepted as Sunday)."""


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

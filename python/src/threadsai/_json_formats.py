"""`format` and `multipleOf` for semantic rule 20, defined exactly as spec/schema/README.md defines
them, so the TypeScript reader (validate/formats.ts) answers the same for every value.

Formats: RFC 3339 `date`, `time` (with its offset) and `date-time`, `email`, absolute `uri` and
`uuid`. Every pattern spells its character classes out: `\\d` and `\\s` mean different things in
Python and ECMAScript regular expressions."""

import re
from collections.abc import Callable, Mapping
from typing import Final

_DATE: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIME: Final = re.compile(
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?([Zz]|[+-]([0-9]{2}):([0-9]{2}))"
)
_EMAIL: Final = re.compile(r"[^@ \t\n\r\f\v]+@[^@. \t\n\r\f\v]+(\.[^@. \t\n\r\f\v]+)+")
_URI: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:[^ \t\n\r\f\v]*")
_UUID: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LONG_MONTHS: Final = frozenset({1, 3, 5, 7, 8, 10, 12})
_DECIMAL: Final = re.compile(r"(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?")


def _date(text: str) -> bool:
    found = _DATE.fullmatch(text)
    if found is None:
        return False
    year, month, day = (int(g) for g in found.groups())
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = 31 if month in _LONG_MONTHS else 30 if month != 2 else 29 if leap else 28  # noqa: PLR2004 - calendar
    return 1 <= month <= 12 and 1 <= day <= days  # noqa: PLR2004 - calendar


def _time(text: str) -> bool:
    found = _TIME.fullmatch(text)
    if found is None:
        return False
    hour, minute, second, _, _, off_hour, off_minute = found.groups()
    clock = int(hour) <= 23 and int(minute) <= 59 and int(second) <= 59  # noqa: PLR2004
    offset = off_hour is None or (int(off_hour) <= 23 and int(off_minute) <= 59)  # noqa: PLR2004
    return clock and offset


def _date_time(text: str) -> bool:
    date, sep, time = text[:10], text[10:11], text[11:]
    return sep in ("T", "t") and _date(date) and _time(time)


FORMATS: Final[Mapping[str, Callable[[str], bool]]] = {
    "date": _date,
    "time": _time,
    "date-time": _date_time,
    "email": lambda text: _EMAIL.fullmatch(text) is not None,
    "uri": lambda text: _URI.fullmatch(text) is not None,
    "uuid": lambda text: _UUID.fullmatch(text) is not None,
}
"""The formats rule 20 checks, each on a string only; any other format is unsupported."""


def _decimal(number: int | float) -> tuple[int, int]:
    """The number's shortest round-trip decimal as (digits, exponent): value = digits * 10**exp.
    Python's repr and ECMAScript's String() write the same digits."""
    found = _DECIMAL.fullmatch(repr(number))
    if found is None:
        raise TypeError(f"not a finite number: {number!r}")
    sign, whole, fraction, exponent = found.groups()
    fraction = fraction or ""
    digits = int(whole + fraction) * (-1 if sign else 1)
    return digits, int(exponent or 0) - len(fraction)


def multiple_of(value: int | float, divisor: int | float) -> bool:
    """Whether `value` is an integer multiple of `divisor`, exactly in decimal (0.3 is a multiple
    of 0.1), never by float division."""
    (a, p), (b, q) = _decimal(value), _decimal(divisor)
    if b <= 0:
        raise TypeError(f"multipleOf must be positive: {divisor!r}")
    low = min(p, q)
    return (a * 10 ** (p - low)) % (b * 10 ** (q - low)) == 0

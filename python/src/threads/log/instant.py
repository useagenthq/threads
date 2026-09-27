"""Epoch milliseconds as RFC 3339 UTC with milliseconds, spelled exactly as JavaScript's
`Date.toISOString` writes it. One implementation, because the spelling crosses boundaries where a
byte matters: a schedule occurrence id is part of an event, and the UI's and A2A's timestamps are
answered by whichever language serves the request."""

from datetime import UTC, datetime


def iso(ms: int) -> str:
    return f"{datetime.fromtimestamp(ms // 1000, UTC):%Y-%m-%dT%H:%M:%S}.{ms % 1000:03d}Z"

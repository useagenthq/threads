"""DST occurrence rules against the shared vector spec/conformance/vectors/schedule-dst.json."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from threads.host.schedules import MINUTE_MS, is_due, parse_cron

VECTOR = Path(__file__).resolve().parents[3] / "spec/conformance/vectors/schedule-dst.json"


class Case(BaseModel):
    name: str
    cron: str
    timezone: str
    from_: str
    to: str
    occurrences: list[str]

    model_config = {"populate_by_name": True, "alias_generator": lambda f: f.rstrip("_")}


class Vector(BaseModel):
    cases: list[Case]


CASES = Vector.model_validate_json(VECTOR.read_text(encoding="utf-8")).cases


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_occurrences_match_the_shared_vector(case: Case) -> None:
    cron = parse_cron(case.cron)
    start, end = _ms(case.from_), _ms(case.to)
    fired = [
        t for t in range(start + MINUTE_MS, end + 1, MINUTE_MS) if is_due(cron, case.timezone, t)
    ]
    assert [_iso(t) for t in fired] == case.occurrences

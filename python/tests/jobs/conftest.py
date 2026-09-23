"""Crash drills never leave a worker process behind, whatever the drill's outcome."""

from collections.abc import Iterator

import pytest
from jobs.drill import reap


@pytest.fixture(autouse=True)
def reaped() -> Iterator[None]:
    yield
    reap()

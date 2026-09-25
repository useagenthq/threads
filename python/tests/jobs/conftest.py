"""Crash drills never leave a worker process behind, whatever the drill's outcome."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from jobs.drill import reap
from jobs.stores import drop


@pytest.fixture(autouse=True)
def reaped(tmp_path: Path) -> Iterator[None]:
    yield
    reap()
    drop(tmp_path)

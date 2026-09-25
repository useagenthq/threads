"""Suite-wide setup: no test can reach a real model provider (AGENTS.md, Tests), and each test
starts with no resolved secrets, so one test's short fake value is never redacted in another's.
With THREADS_TEST_STORE=postgres (lane 27), the conformance and team-op vector runners open every
in-memory store as a fresh Postgres schema."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pg_kit import ENGINE, postgres_memory

from threads.loop.guard import block_model_requests
from threads.redaction import forget_secrets

block_model_requests()

RUNNERS = frozenset(
    {
        "log/test_conformance.py",
        "reduce/test_reduce_cases.py",
        "reduce/test_staged_cases.py",
        "host/test_host_cases.py",
        "host/test_intake_cases.py",
        "loop/test_loop_cases.py",
        "thread/test_fork_cases.py",
        "team/test_team_cases.py",
        "team/test_ops_vectors.py",
    }
)


@pytest.fixture(autouse=True)
def _no_resolved_secrets() -> Iterator[None]:
    yield
    forget_secrets()


@pytest.fixture(autouse=True)
def _engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    here = Path(str(request.path)).relative_to(Path(__file__).parent).as_posix()
    if ENGINE != "postgres" or here not in RUNNERS:
        yield
        return
    with postgres_memory(monkeypatch):
        yield

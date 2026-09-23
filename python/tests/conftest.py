"""Suite-wide setup: no test can reach a real model provider (AGENTS.md, Tests), and each test
starts with no resolved secrets, so one test's short fake value is never redacted in another's."""

from collections.abc import Iterator

import pytest

from threads import redaction
from threads.loop.guard import block_model_requests

block_model_requests()


@pytest.fixture(autouse=True)
def _no_resolved_secrets() -> Iterator[None]:
    yield
    redaction._REGISTERED.clear()  # pyright: ignore[reportPrivateUsage] - reset between tests

"""exa, tavily and brave with an unset key: each search answers unavailable, naming the
variable, and sends nothing (the same in TypeScript). The run goes on; only setup raises."""

import asyncio
import dataclasses
from collections.abc import Callable

import pytest

from threads.result import Err, Ok
from threads.search import HttpSearch, brave, exa, tavily
from threads.secrets import Secret, secret
from threads.web.guard import Target
from threads.web.http import Fence, Request, Response, WebError


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[Request] = []

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        self.sent.append(request)
        return Ok(Response(200, {}, b'{"results": []}'))


@pytest.mark.parametrize("make", [exa, tavily, brave], ids=["exa", "tavily", "brave"])
def test_each_search_is_unavailable_names_the_variable_and_sends_nothing(
    make: Callable[[Secret], HttpSearch], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("THREADS_TEST_UNSET_KEY", raising=False)
    recorder = _Recorder()
    backend = dataclasses.replace(make(secret("THREADS_TEST_UNSET_KEY")), transport=recorder)
    got = asyncio.run(backend.search("q"))
    assert isinstance(got, Err)
    assert got.error.code == "unavailable"
    assert "THREADS_TEST_UNSET_KEY" in got.error.message
    assert recorder.sent == []

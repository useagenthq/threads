"""exa, tavily and brave with an unset key. The key resolves at setup, so check() reports
missing_secret before any run (the <factory>!missing_secret@py tests of spec/api.json); a search
sent without setup answers unavailable, naming the variable, and sends nothing. The same in
TypeScript."""

import asyncio
import dataclasses
from collections.abc import Callable, Sequence

import pytest

from threads import Failure, agent, scripted_model
from threads.memory.fence import bound
from threads.result import Err, Ok
from threads.search import HttpSearch, brave, exa, tavily
from threads.secrets import Secret, secret
from threads.web.guard import Target
from threads.web.http import Fence, Request, Response, WebError

FACTORIES = pytest.mark.parametrize("make", [exa, tavily, brave], ids=["exa", "tavily", "brave"])
UNSET = "THREADS_TEST_UNSET_KEY"


async def _public(_host: str, _port: int) -> Sequence[str]:
    return ["93.184.216.34"]


async def _open() -> bool:
    return True


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[Request] = []

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        self.sent.append(request)
        return Ok(Response(200, {}, b'{"results": []}'))


def _backend(make: Callable[[Secret], HttpSearch], recorder: _Recorder) -> HttpSearch:
    return dataclasses.replace(make(secret(UNSET)), transport=recorder)


@FACTORIES
def test_check_reports_an_unset_key_as_missing_secret(
    make: Callable[[Secret], HttpSearch], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(UNSET, raising=False)
    recorder = _Recorder()
    bot = agent(
        name="searcher",
        model=scripted_model({"responses": []}),
        web={"search": _backend(make, recorder)},
    )
    name = make(secret(UNSET)).factory
    expected = Err(Failure("missing_secret", f"{name}: set api_key or {UNSET}"))
    assert asyncio.run(bot.check()) == expected
    assert recorder.sent == []


def test_a_key_resolved_at_setup_is_kept_for_the_searches_that_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UNSET, "sk-kept-for-searches")
    recorder = _Recorder()
    backend = dataclasses.replace(_backend(exa, recorder), resolver=_public)
    asyncio.run(backend.setup())
    monkeypatch.delenv(UNSET)

    async def search() -> object:
        with bound(_open):
            return await backend.search("q")

    assert isinstance(asyncio.run(search()), Ok)
    assert recorder.sent[0].headers["x-api-key"] == "sk-kept-for-searches"


@FACTORIES
def test_without_setup_a_search_is_unavailable_names_the_variable_and_sends_nothing(
    make: Callable[[Secret], HttpSearch], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(UNSET, raising=False)
    recorder = _Recorder()
    got = asyncio.run(_backend(make, recorder).search("q"))
    assert isinstance(got, Err)
    assert got.error.code == "unavailable"
    assert UNSET in got.error.message
    assert recorder.sent == []

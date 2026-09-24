"""`Agent.run_sync`: a blocking run for scripts, on a new event loop in the calling thread, and
one agent run on several loops in turn without stale connections."""

import asyncio
import gc
import sqlite3
import threading
from pathlib import Path

import pytest
from loop_kit import Sessions, bash_agent, daytona, text
from pydantic import BaseModel

from threads import (
    Completed,
    ConfigError,
    RunContext,
    RunResult,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.cli import store as cli_store
from threads.host import host

TWO_LOOPS = 2


class Nothing(BaseModel):
    pass


def test_run_sync_returns_what_run_returns() -> None:
    bot = agent(model=scripted_model({"responses": [text("Hello!")]}), instructions="Greet.")
    result: RunResult[str] = bot.run_sync("hi", store=sqlite(":memory:"))
    assert isinstance(result, Completed)
    assert result.output == "Hello!"


def test_run_sync_inside_a_loop_raises_and_touches_nothing() -> None:
    bot = agent(model=scripted_model({"responses": [text("never")]}))
    store = sqlite(":memory:")

    async def main() -> None:
        with pytest.raises(ConfigError) as raised:
            bot.run_sync("hi", store=store)
        assert raised.value.code == "invalid_config"
        assert "await agent.run" in raised.value.message

    before = threading.active_count()
    asyncio.run(main())  # warnings are errors: an un-awaited run coroutine would fail here
    assert threading.active_count() == before  # no store thread was started


def test_missing_deps_is_config_error() -> None:
    async def run(_args: Nothing, _ctx: RunContext[int]) -> str:
        return "x"

    noop = tool(name="noop", description="d", input=Nothing, execute=run, effect="read_only")
    bot = agent(model=scripted_model({"responses": []}), tools=[noop])
    with pytest.raises(ConfigError) as raised:
        bot.run_sync("hi", store=sqlite(":memory:"))
    assert raised.value.code == "invalid_config"


def _two_sessions_both_closed(sessions: Sessions) -> None:
    gc.collect()  # an unclosed session warns when collected; warnings are errors here
    assert len(sessions.seen) == TWO_LOOPS
    assert all(s.closed for s in sessions.seen)


def test_two_run_sync_calls_through_daytona(tmp_path: Path) -> None:
    sessions = Sessions()
    with daytona(sessions) as box:
        bot = bash_agent(box, runs=2)
        for _ in range(2):
            result = bot.run_sync("go", store=sqlite(str(tmp_path)))
            assert isinstance(result, Completed), result
    _two_sessions_both_closed(sessions)


def test_two_raw_asyncio_runs_need_no_cleanup_code(tmp_path: Path) -> None:
    sessions = Sessions()
    with daytona(sessions) as box:
        bot = bash_agent(box, runs=2)
        for _ in range(2):
            result = asyncio.run(bot.run("go", store=sqlite(str(tmp_path))))
            assert isinstance(result, Completed), result
            assert sessions.seen[-1].closed  # closed by the run's own release, on its loop
    _two_sessions_both_closed(sessions)


def test_run_sync_from_two_threads(tmp_path: Path) -> None:
    sessions = Sessions()
    results: list[RunResult[str]] = []
    with daytona(sessions) as box:
        store = sqlite(str(tmp_path))

        # One sandbox adapter, used by both threads at once (each agent has its own script).
        def run_one() -> None:
            results.append(bash_agent(box, runs=1).run_sync("go", store=store))

        workers = [threading.Thread(target=run_one) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    assert [r.status for r in results] == ["completed", "completed"]
    _two_sessions_both_closed(sessions)


def test_host_holds_connections_between_runs(tmp_path: Path) -> None:
    sessions = Sessions()
    with daytona(sessions) as box:
        bot = bash_agent(box, runs=2)
        store = sqlite(str(tmp_path))

        async def main() -> None:
            async with host(store=store, agents={"bot": bot}):
                for _ in range(2):
                    assert isinstance(await bot.run("go", store=store), Completed)
                (session,) = sessions.seen  # both runs used one session
                assert not session.closed
            assert session.closed  # stop() released the host's hold

        asyncio.run(main())


def test_cli_gc_closes_what_it_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gc loads the host's adapters without ready() and holds the loop itself."""
    sessions = Sessions()
    with daytona(sessions) as box:
        bot = bash_agent(box, runs=1)
        store = sqlite(str(tmp_path))
        assert isinstance(bot.run_sync("go", store=store), Completed)
        with sqlite3.connect(tmp_path / "threads.db") as db:  # its sandbox expired: collectable
            expired = "UPDATE resources SET expires_at = 0 WHERE state = 'live'"
            assert db.execute(expired).rowcount >= 1
        served = host(store=store, agents={"bot": bot})
        monkeypatch.setattr(cli_store, "load", lambda _module: served)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType] - load's stand-in
        assert asyncio.run(cli_store.gc(str(tmp_path), "app", 1.0)) == 0
    assert len(sessions.seen) == TWO_LOOPS
    assert sessions.seen[-1].closed

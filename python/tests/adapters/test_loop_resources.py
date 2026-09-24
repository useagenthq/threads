"""The owner of loop-bound adapter resources: bundles per loop, released when the last hold
on a loop ends (retire, drain, close), and every background task's outcome collected once."""

import asyncio
import logging
from dataclasses import dataclass, field

import pytest
from loop_kit import Handled

from threads.adapters import loop_resources
from threads.adapters.loop_resources import KEPT, LoopResources, holding, spawn_owned


@dataclass
class Bundle:
    """A test client that fails loudly when used after its release retired it."""

    label: str
    closed: bool = False
    retired: bool = False

    def use(self) -> None:
        assert not self.retired, f"{self.label} used after it was retired"


@dataclass
class Adapter:
    name: str = "test"
    made: list[Bundle] = field(default_factory=list[Bundle])
    closing: list[str] = field(default_factory=list[str])
    gate: asyncio.Event | None = None
    fail: bool = False

    def __post_init__(self) -> None:
        self.bundles = LoopResources(self.name, self._close)

    def bundle(self) -> Bundle:
        def make() -> Bundle:
            made = Bundle(f"{self.name}{len(self.made)}")
            self.made.append(made)
            return made

        found = self.bundles.get(make)
        found.use()
        return found

    async def _close(self, bundle: Bundle) -> None:
        bundle.retired = True
        self.closing.append(bundle.label)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError(f"{self.name} refused to close")
        bundle.closed = True


def test_loop_resources_unit() -> None:
    """Bundles are per loop; release to zero closes and removes that loop's entries only."""
    adapter = Adapter()

    async def on_loop() -> Bundle:
        async with holding():
            first = adapter.bundle()
            assert adapter.bundle() is first  # one bundle per loop
            async with holding():  # a nested hold: nothing closes when it ends
                assert adapter.bundle() is first
            assert not first.closed
            return first

    one = asyncio.run(on_loop())
    two = asyncio.run(on_loop())  # a new loop (maybe at the dead one's id) gets a new bundle
    assert one is not two
    assert one.closed
    assert two.closed
    assert loop_resources._entries == {}  # pyright: ignore[reportPrivateUsage] - the registry


def test_a_get_with_no_hold_is_a_bug() -> None:
    async def main() -> None:
        with pytest.raises(AssertionError, match="no hold"):
            Adapter().bundle()

        async def never() -> None:
            pass

        with pytest.raises(AssertionError, match="no hold"):
            spawn_owned(never(), "never")  # closed, so no un-awaited coroutine warning

    asyncio.run(main())


def test_a_new_hold_during_release_gets_a_new_bundle() -> None:
    adapter = Adapter()

    async def main() -> None:
        adapter.gate = asyncio.Event()

        async def run_one() -> Bundle:
            async with holding():
                return adapter.bundle()

        first = asyncio.create_task(run_one())
        while adapter.closing != ["test0"]:  # run 1 ended; its release waits in close
            await asyncio.sleep(0)
        async with holding():
            second = adapter.bundle()  # run 2, on the same loop, while run 1 releases
            assert second is not adapter.made[0]
            adapter.gate.set()
            retired = await first
            assert retired.closed
            assert not second.closed
            second.use()
        assert second.closed

    asyncio.run(main())


def test_every_closer_runs_when_one_fails(caplog: pytest.LogCaptureFixture) -> None:
    left, middle, right = Adapter("left"), Adapter("middle", fail=True), Adapter("right")

    async def ok() -> str:
        async with holding():
            for adapter in (left, middle, right):
                adapter.bundle()
        return "done"

    with caplog.at_level(logging.WARNING, "threads"):
        assert asyncio.run(ok()) == "done"
    assert left.made[0].closed
    assert right.made[0].closed
    (warning,) = [r.getMessage() for r in caplog.records]
    assert "closing middle: RuntimeError: middle refused to close" in warning

    async def failing() -> None:
        async with holding():
            middle.bundle()
            raise ValueError("the run failed")

    with pytest.raises(ValueError, match="the run failed") as raised:
        asyncio.run(failing())
    assert any("middle refused to close" in note for note in raised.value.__notes__)


async def _boom() -> None:
    raise RuntimeError("the provider refused")


def _failure_lines(caplog: pytest.LogCaptureFixture) -> tuple[list[str], list[str]]:
    """The failures logged when collected, and those reported at release."""
    lines = [r.getMessage() for r in caplog.records]
    return (
        [line for line in lines if "background task failed" in line],
        [line for line in lines if "while closing connections" in line],
    )


@pytest.mark.parametrize("timing", ["before_release", "during_drain", "callback_queued"])
def test_a_failing_owned_task_is_collected_once(
    timing: str, caplog: pytest.LogCaptureFixture
) -> None:
    handled = Handled()
    adapter = Adapter()

    async def slow_boom() -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("the provider refused")

    async def main() -> None:
        handled.install()
        async with holding():
            adapter.bundle()
            match timing:
                case "before_release":
                    task = spawn_owned(_boom(), "terminate k1")
                    await asyncio.sleep(0.01)  # it failed and its callback ran
                    assert task.done()
                case "during_drain":
                    spawn_owned(slow_boom(), "terminate k1")  # still running at release
                case _:
                    # Done, with its done callback still queued when release collects it.
                    task = spawn_owned(_boom(), "terminate k1")
                    await asyncio.sleep(0)  # the task runs and fails; its callback is queued
                    assert task.done()

    with caplog.at_level(logging.WARNING, "threads"):
        asyncio.run(main())
    logged, reported = _failure_lines(caplog)
    assert len(logged) == 1
    assert "terminate k1: RuntimeError: the provider refused" in logged[0]
    assert len(reported) == 1
    assert "the provider refused" in reported[0]
    assert adapter.made[0].closed  # every closer still ran
    assert not [m for m in handled.messages if "never retrieved" in m]


def test_failures_kept_under_a_long_hold_are_bounded(caplog: pytest.LogCaptureFixture) -> None:
    failed = 10_000

    async def main() -> None:
        async with holding():  # a host's hold, across many failures
            for _ in range(failed):
                await asyncio.wait({spawn_owned(_boom(), "terminate k")})
            await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING, "threads"):
        asyncio.run(main())
    logged, reported = _failure_lines(caplog)
    assert len(logged) == failed  # each logged once, when it happened
    assert len(reported) == KEPT + 1
    assert reported[-1].endswith(f"{failed - KEPT} more failures were logged earlier")

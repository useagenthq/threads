"""`terminate(K)`: ending one command's process group, and saying whether it ended (lane 16 §D-2).

`supervise --terminate <key-hash>` is the only probe, and its exit status is the contract
(docker/supervise/supervise.h), measured against the live daemon. This module is that table.
Everything it decides from comes out of records.py — one archive GET, no guessing from a scan
the guest could forge.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

from threads.adapters.sandboxes.docker import exec as docker_exec
from threads.adapters.sandboxes.docker.engine import Engine, EngineError
from threads.adapters.sandboxes.docker.records import SUPERVISE, State, answered, read_state
from threads.loop.tools import Termination

_ROUNDS: Final = 3
"""How often a probe that answered "K's supervisor is gone" is asked again."""
_STOP_GRACE_S: Final = 5.0


class Ex:
    """`supervise --terminate` exit statuses (docker/supervise/supervise.h)."""

    NOT_LIVE: Final = 0
    KILLED: Final = 10
    SIGNALLED: Final = 11
    GONE: Final = 12
    BUSY: Final = 13


@dataclass(frozen=True, slots=True)
class Pacing:
    """How long the table waits for the container to settle, and how often it looks."""

    poll_s: float = 0.2
    wait_s: float = 10.0


async def terminate(engine: Engine, container: str, key_hash: str, pacing: Pacing) -> Termination:
    """Ends the key's whole process group and says whether it did (driver.py `Confirmed`)."""
    for _ in range(_ROUNDS):
        state = await _state_or_stopped(engine, container)
        if state is None:
            return "terminated"  # the container is gone or stopped: nothing in it survived
        if key_hash in state.records and state.live(key_hash) is None:
            return answered(state, key_hash)
        # Either K may still be running, or it has no record yet: the supervisor writes one
        # before it lets the child run, and an exec start returns a moment earlier. The probe
        # is the authority on both.
        answer = await _after_the_probe(engine, container, key_hash, pacing, state)
        if answer is not None:
            return answer
    return "unknown"


async def _after_the_probe(
    engine: Engine, container: str, key_hash: str, pacing: Pacing, state: State
) -> Termination | None:
    """One round of the table. None asks again: K's supervisor died, or the stop left a
    `stuck` record."""
    match await _probe(engine, container, key_hash):
        case Ex.NOT_LIVE:
            later = await _state_or_stopped(engine, container)
            return "terminated" if later is None else answered(later, key_hash)
        case Ex.GONE:
            return None
        case Ex.KILLED:
            return await _stopped(engine, container, pacing)
        case Ex.SIGNALLED:
            return await _settled(engine, container, key_hash, pacing, state)
        case Ex.BUSY:
            return await _past_the_deadline(engine, container, key_hash, pacing, state)
        case _:
            return "unknown"  # the probe itself failed


async def _probe(engine: Engine, container: str, key_hash: str) -> int:
    """`supervise --terminate`, as uid 0 and taking no lock. Exit 10 usually never reaches the
    host: killing `--idle` kills the pid namespace, so the exec dies with it. An exec that ended
    without an exit code is read the same way."""
    cmd = (SUPERVISE, "--terminate", key_hash)
    try:
        code, _, _ = await docker_exec.run_to_end(engine, container, cmd, "0")
    except (docker_exec.FrameError, EngineError):
        return Ex.KILLED
    return code


async def _state_or_stopped(engine: Engine, container: str) -> State | None:
    """None: the container is gone or stopped, so every process in it is gone."""
    found = await engine.inspect_container(container)
    if found is None or not found.state.running:
        return None
    return await read_state(engine, container)


async def _stopped(engine: Engine, container: str, pacing: Pacing) -> Termination:
    """Row 10: the probe took the lock and killed `--idle`. The container stopping is the
    proof; without it nothing is proven."""
    async for _ in _ticks(pacing.wait_s, pacing.poll_s):
        found = await engine.inspect_container(container)
        if found is None or not found.state.running:
            return "terminated"
    return "unknown"


async def _settled(
    engine: Engine, container: str, key_hash: str, pacing: Pacing, seen: State
) -> Termination | None:
    """Row 11: a stop request was written and K's supervisor signalled. None: the record went
    `stuck`, so the probe is asked again."""
    async for _ in _ticks(_bounded(pacing, seen, key_hash, _STOP_GRACE_S), pacing.poll_s):
        state = await _state_or_stopped(engine, container)
        if state is None:
            return "terminated"
        still = state.live(key_hash)
        if still is None:
            return answered(state, key_hash)
        if still.state == "stuck":
            return None
    return "unknown"


async def _past_the_deadline(
    engine: Engine, container: str, key_hash: str, pacing: Pacing, seen: State
) -> Termination:
    """Row 13: the lock is held by someone other than K's supervisor. K's own deadline bounds
    the wait; still running after it proves nothing."""
    async for _ in _ticks(_bounded(pacing, seen, key_hash, _STOP_GRACE_S), pacing.poll_s):
        state = await _state_or_stopped(engine, container)
        if state is None:
            return "terminated"
        if state.live(key_hash) is None:
            return answered(state, key_hash)
    return "unknown"


def _bounded(pacing: Pacing, seen: State, key_hash: str, grace_s: float) -> float:
    """K's deadline plus a grace, never longer than the adapter's own wait."""
    record = seen.live(key_hash)
    if record is None:
        return pacing.wait_s
    return min(pacing.wait_s, record.deadline_ms / 1000 + grace_s)


async def _ticks(seconds: float, poll_s: float) -> AsyncIterator[None]:
    """One look now, then more until `seconds` have passed."""
    until = time.monotonic() + seconds
    yield None
    while time.monotonic() < until:
        await asyncio.sleep(poll_s)
        yield None

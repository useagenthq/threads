"""What the supervisor recorded, and what `terminate` answers from it (lane 16 §D-2).

Records are read with one archive GET of `state/`, never a probe: it works on a stopped
container and when pids are exhausted, and it carries `state/generation` in the same answer, so
a record left by an older container start is classified without asking anyone. Every record is
parsed strictly (wire.Record); anything else is `unavailable`.

`supervise --terminate <key-hash>` is the only probe, and its exit status is the contract
(docker/supervise/supervise.h). This module is that table.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from threads.adapters.sandboxes.docker import archive
from threads.adapters.sandboxes.docker import exec as docker_exec
from threads.adapters.sandboxes.docker.engine import NOT_FOUND, Engine, EngineError
from threads.adapters.sandboxes.docker.wire import Record
from threads.loop.tools import Termination

STATE_DIR: Final = "/run/threads/state"
SUPERVISE: Final = "/run/threads/bin/supervise"
_GENERATION: Final = "state/generation"
_RECORDS: Final = "state/records/"
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


@dataclass(frozen=True, slots=True)
class State:
    """One archive GET of `state/`: this container start's generation, and every record."""

    generation: str
    records: Mapping[str, Record]

    def live(self, key_hash: str) -> Record | None:
        """K's record when it may still have processes: `running` or `stuck` in this
        generation. An older generation's is dead — the container stopped, so every process in
        it died — and is never rewritten."""
        found = self.records.get(key_hash)
        if found is None or found.generation != self.generation:
            return None
        return found if found.state in ("running", "stuck") else None


async def read_state(engine: Engine, container: str) -> State:
    """Raises EngineError when the container is gone: its processes are gone with it."""
    got = await engine.get_archive(container, STATE_DIR)
    if got is None:
        raise EngineError(NOT_FOUND, f"{container} has no {STATE_DIR}")
    files = archive.read_tar(got)
    generation = files.get(_GENERATION, b"").decode("utf-8", "replace").strip()
    records: dict[str, Record] = {}
    for name, data in files.items():
        if name.startswith(_RECORDS) and name.endswith(".json"):
            key = name.removeprefix(_RECORDS).removesuffix(".json")
            records[key] = _record(name, data)
    return State(generation, records)


def _record(name: str, data: bytes) -> Record:
    try:
        return Record.model_validate_json(data)
    except ValidationError as broken:
        raise archive.ArchiveError(f"{name} is not a record: {broken}") from broken


def answered(state: State, key_hash: str) -> Termination:
    """What a record alone proves. A record that may still have processes proves nothing, and
    neither does a missing one: nothing ran under this key, or its record is gone (D-3, the
    call parks)."""
    found = state.records.get(key_hash)
    if found is None or state.live(key_hash) is not None:
        return "unknown"
    if found.state == "exited":
        return "already_exited"
    # terminated, or a running/stuck record of an older generation: the container stopped.
    return "terminated"


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

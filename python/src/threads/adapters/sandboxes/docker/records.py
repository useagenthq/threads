"""What the supervisor recorded about each command, read back from the container.

Records are read with one archive GET of `state/`, never a probe: it works on a stopped
container and when pids are exhausted, and it carries `state/generation` in the same answer, so
a record left by an older container start is classified without asking anyone. Every record is
parsed strictly (wire.Record); anything else is `unavailable`.

The record is also where a command's own exit code lives: `supervise` always exits 0 itself
(docker/supervise/run.c `mode_run`), so the exec's status is the supervisor's, never the
command's. terminate.py reads these for the D-2 table; exec.py reads them for the exit code.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, assert_never

from pydantic import ValidationError

from threads.adapters.sandboxes.docker import archive
from threads.adapters.sandboxes.docker.engine import NOT_FOUND, Engine, EngineError
from threads.adapters.sandboxes.docker.wire import Record
from threads.loop.tools import Termination

STATE_DIR: Final = "/run/threads/state"
SUPERVISE: Final = "/run/threads/bin/supervise"
_GENERATION: Final = "state/generation"
_RECORDS: Final = "state/records/"


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


ADMISSION_REFUSED: Final = 125
KILLED: Final = 137


class SupervisorError(Exception):
    """A keyed command whose outcome the supervisor never recorded: it may not have run, or it
    may still have processes. Never reported as an exit code the command chose."""


def exit_code(state: State, key_hash: str, status: int) -> int:
    """A keyed command's own exit code.

    `supervise` always exits 0 itself (docker/supervise/run.c `mode_run`), so `status` — the
    exec's — only says whether the supervisor got as far as running anything, and the command's
    code is in its record. Anything the record leaves in doubt raises rather than inventing a
    code the command never returned.
    """
    if status == ADMISSION_REFUSED:
        raise SupervisorError("another command is still running in this sandbox; nothing ran")
    if status != 0:
        raise SupervisorError(f"the supervisor exited {status} without running the command")
    found = state.records.get(key_hash)
    if found is None or found.generation != state.generation:
        raise SupervisorError("the command left no record of this container start")
    match found.state:
        case "exited":
            return found.exit_code
        case "terminated":
            # The deadline or a stop request: the group was killed, never a normal exit.
            return KILLED
        case "running" | "stuck":
            raise SupervisorError(f"the command's record is still {found.state}")
        case _:
            assert_never(found.state)


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

"""The Docker exec path: how the container's multiplexed stream is parsed, and where a
command's own exit code comes from — its record, never `supervise`'s own status."""

import asyncio
from collections.abc import Mapping

import pytest
from docker_fake import DockerEngine, adapter
from pydantic import JsonValue
from sandbox_backend import FakeBackend
from sandbox_kit import OPEN

from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes.docker import records
from threads.adapters.sandboxes.docker.exec import FRAME_MAX, FrameError, Frames
from threads.adapters.sandboxes.posix import collect
from threads.adapters.sandboxes.streams import StreamLostError
from threads.result import Ok
from threads.sandbox import SandboxSession, Trees
from threads.sandbox.protocol import ExecOutput

pytestmark = pytest.mark.usefixtures("stub_supervisor")

BOOM: Mapping[str, JsonValue] = {"tools": {"boom": {"output": "no", "is_error": True}}}
"""A scripted tool that exits 1, so a test can tell a real code from a defaulted 0."""


def test_the_supervisor_runs_a_keyed_command_and_the_kit_s_own_scripts_do_not() -> None:
    """A keyed command goes through the supervisor as uid 0, which drops its child to 1000.
    The kit's fixed tree scripts carry no key: they run as uid 1000 and take no lock."""

    async def main() -> list[tuple[str, tuple[str, ...]]]:
        backend = FakeBackend.scripted()
        engine = DockerEngine(backend)
        async with holding():
            made = await adapter(backend, "docker", engine).create("k", OPEN)
            assert isinstance(made, Ok), made
            session: SandboxSession = made.value
            assert isinstance(session, Trees)
            assert isinstance(await session.exec(["echo", "hi"], OPEN, process_key="p"), Ok)
            assert isinstance(await session.export_tree(OPEN), Ok)
        return engine.started

    started = asyncio.run(main())
    supervised = [(user, cmd) for user, cmd in started if cmd[0] == records.SUPERVISE]
    assert [user for user, _ in supervised] == ["0", "0"]  # --check, then the keyed command
    assert [cmd[:1] for user, cmd in started if user == "1000"] == [("/bin/sh",)]


async def _keyed(engine: DockerEngine, backend: FakeBackend, argv: list[str]) -> ExecOutput:
    """One keyed command over the mocked daemon, its ExecOutput left unread."""
    made = await adapter(backend, "docker", engine).create("k", OPEN)
    assert isinstance(made, Ok), made
    started = await made.value.exec(argv, OPEN, process_key="p")
    assert isinstance(started, Ok), started
    return started.value


def test_a_commands_exit_code_comes_from_its_record_not_the_supervisors() -> None:
    """`supervise` always exits 0 itself (docker/supervise/run.c `mode_run`), so the exec's
    status is never the command's."""

    async def main() -> tuple[int, bytes, bytes]:
        backend = FakeBackend.scripted(BOOM)
        engine = DockerEngine(backend)
        async with holding():
            return await collect(await _keyed(engine, backend, ["boom"]))

    assert asyncio.run(main())[0] == 1


def test_a_terminated_command_never_reads_as_a_normal_exit() -> None:
    """The record says `terminated`, so the exit is the kill, not whatever memset left."""

    async def main() -> int:
        backend = FakeBackend.scripted()
        engine = DockerEngine(backend)
        async with holding():
            made = await adapter(backend, "docker", engine).create("k", OPEN)
            assert isinstance(made, Ok), made
            started = await made.value.exec(["sleep", "100"], OPEN, process_key="p")
            assert isinstance(started, Ok), started
            assert await made.value.terminate("p", OPEN) == Ok("terminated")
            return (await collect(started.value))[0]

    assert asyncio.run(main()) == 137  # noqa: PLR2004 - SIGKILL


@pytest.mark.parametrize("knob", ["refuse_admission", "drop_records"], ids=["refused", "no_record"])
def test_an_outcome_the_record_leaves_in_doubt_is_never_an_exit_code(knob: str) -> None:
    """Admission refused means nothing ran; a missing record means nothing is known. Neither
    is reported as a code the command chose."""

    async def main() -> None:
        backend = FakeBackend.scripted()
        engine = DockerEngine(backend)
        setattr(engine, knob, True)
        async with holding():
            output = await _keyed(engine, backend, ["echo", "hi"])
            with pytest.raises(StreamLostError):
                await collect(output)

    asyncio.run(main())


@pytest.mark.parametrize(
    ("knob", "why"),
    [("refuse_admission", "nothing ran"), ("supervisor_dies", "exited 1")],
    ids=["admission_refused", "supervisor_died"],
)
def test_a_key_s_earlier_record_never_answers_for_an_attempt_that_did_not_run(
    knob: str, why: str
) -> None:
    """Nothing ever unlinks a record (docker/supervise/), so a key that ran before still has
    one in this generation, and a retry reuses the key (it is the effect key). Every nonzero
    supervisor status is pre-record — 125 for a refused admission, 1 for every `die()` — so
    the status is read before the record, never after it."""

    async def main() -> None:
        backend = FakeBackend.scripted(BOOM)
        engine = DockerEngine(backend)
        async with holding():
            made = await adapter(backend, "docker", engine).create("k", OPEN)
            assert isinstance(made, Ok), made
            first = await made.value.exec(["boom"], OPEN, process_key="p")
            assert isinstance(first, Ok), first
            assert (await collect(first.value))[0] == 1
            # The same key again: its earlier record still says `exited` 1, and must not answer.
            setattr(engine, knob, True)
            again = await made.value.exec(["boom"], OPEN, process_key="p")
            assert isinstance(again, Ok), again
            with pytest.raises(StreamLostError, match=why):
                await collect(again.value)

    asyncio.run(main())


@pytest.mark.parametrize(
    "wire",
    [
        b"\x09\x00\x00\x00\x00\x00\x00\x01x",
        b"\x01\x01\x00\x00\x00\x00\x00\x01x",
        b"\x01\x00\x00\x00\xff\xff\xff\xffx",
    ],
    ids=["unknown_stream", "dirty_header", "over_long"],
)
def test_a_malformed_frame_is_refused(wire: bytes) -> None:
    with pytest.raises(FrameError):
        Frames().feed(wire)


def test_a_frame_split_anywhere_reaches_its_own_stream() -> None:
    payload = bytes(range(256))
    head = len(payload).to_bytes(4, "big")
    wire = b"\x01\x00\x00\x00" + head + payload + b"\x02\x00\x00\x00" + head + payload[::-1]
    for size in (1, 3, 7, 64, 4096):
        frames = Frames()
        got: dict[str, bytes] = {"stdout": b"", "stderr": b""}
        for chunk in [wire[i : i + size] for i in range(0, len(wire), size)]:
            for stream, data in frames.feed(chunk):
                got[stream] += data
        frames.end()
        assert got == {"stdout": payload, "stderr": payload[::-1]}, size


def test_a_stream_that_ends_inside_a_frame_is_refused() -> None:
    frames = Frames()
    assert frames.feed(b"\x01\x00\x00\x00\x00\x00\x00\x04ab") == []
    with pytest.raises(FrameError):
        frames.end()


def test_the_frame_cap_is_64_mib() -> None:
    assert FRAME_MAX == 64 * 1024 * 1024

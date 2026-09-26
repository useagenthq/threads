"""The Docker adapter over a mocked Engine API (docker_fake.py): the shared sandbox suite, the
ledger's crash and takeover rules, the fork conformance cases, and the boundaries of its own —
the socket, the create sequence, the injected supervisor, and everything a container writes."""

import asyncio
import json

import pytest
from corpus import CASES, cases
from docker_fake import DockerEngine, adapter, make
from fork_kit import assert_expected, assert_restore_refused, reaches_restore, run_case, script_of
from sandbox_backend import FakeBackend
from sandbox_contract import CHECKS, Check, run_check
from sandbox_deadline_kit import DEADLINE
from sandbox_kit import OPEN, KitContext
from sandbox_ledger_kit import LEDGER, Body, run_ledger

from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes.docker import create, records, transport
from threads.adapters.sandboxes.docker.exec import FRAME_MAX, FrameError, Frames
from threads.adapters.sandboxes.docker.sandbox import IMAGE
from threads.agents.config import ConfigError
from threads.result import Err, Ok
from threads.sandbox import SandboxError, SandboxSession, Trees


@pytest.mark.parametrize("check", [*CHECKS, *DEADLINE], ids=lambda c: c.__name__)
def test_contract(check: Check) -> None:
    asyncio.run(run_check(check, make, ()))


@pytest.mark.parametrize("body", LEDGER, ids=lambda b: b.__name__)
def test_ledger(body: Body) -> None:
    asyncio.run(run_ledger(body, make))


@pytest.mark.parametrize("name", cases("fork"))
def test_fork_case(name: str) -> None:
    case = CASES / name

    async def main() -> None:
        backend = FakeBackend.scripted(script_of(case))
        async with make(backend, "fake") as sandbox:
            got = await run_case(case, sandbox, lambda: backend.creates)
        if reaches_restore(name):
            assert_restore_refused(got)  # docker declares no snapshots (16C's host trees are)
        else:
            assert_expected(name, got)

    asyncio.run(main())


def test_no_socket_is_docker_unreachable_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup() resolves the socket on the host and opens nothing (the Sandbox.setup contract)."""
    monkeypatch.delenv(transport.DOCKER_HOST, raising=False)
    monkeypatch.setattr(transport, "socket_exists", _never)
    backend = FakeBackend.scripted()
    sandbox = adapter(backend, "docker")
    with pytest.raises(ConfigError) as refused:
        asyncio.run(sandbox.setup())
    assert refused.value.code == "docker_unreachable"
    assert refused.value.message == (
        "Docker isn't running (looked for /var/run/docker.sock, ~/.docker/run/docker.sock): "
        "start Docker, set DOCKER_HOST, or pass another sandbox (devSandbox(), e2b())"
    )
    assert backend.requests == 0, "setup reached the daemon"


def _never(_path: str) -> bool:
    return False


def test_a_stale_writer_sends_no_byte() -> None:
    """The fence runs at the transport's send point, before any byte of the request."""

    async def main() -> None:
        backend = FakeBackend.scripted()
        stale = KitContext(live=False)
        async with holding():
            got = await adapter(backend, "docker").create("k", stale)
        assert isinstance(got, Err)
        assert got.error.code == "stale_epoch"
        assert (stale.fences, backend.requests) == (1, 0)

    asyncio.run(main())


def test_a_name_already_in_use_is_this_key_s_earlier_create() -> None:
    """A 409 means the unique name is there: that container is the one, and nothing new is
    made."""

    async def main() -> None:
        backend = FakeBackend.scripted()
        engine = DockerEngine(backend)
        async with holding():
            sandbox = adapter(backend, "docker", engine)
            first = await sandbox.create("k", OPEN)
            assert isinstance(first, Ok), first
            engine.conflict_names = {first.value.id}
            again = await sandbox.create("k", OPEN)
            assert isinstance(again, Ok), again
            assert again.value.id == first.value.id
        assert backend.creates == 1

    asyncio.run(main())


def test_a_missing_image_is_pulled_anonymously_and_the_create_retried() -> None:
    async def main() -> None:
        backend = FakeBackend.scripted()
        engine = DockerEngine(backend)
        engine.missing_image = IMAGE
        async with holding():
            made = await adapter(backend, "docker", engine).create("k", OPEN)
        assert isinstance(made, Ok), made
        assert engine.pulls == [IMAGE]

    asyncio.run(main())


@pytest.mark.parametrize(
    "warning",
    [
        "Your kernel does not support memory limit capabilities. Limitation discarded.",
        "Your kernel does not support CPU cfs quota. Limitation discarded.",
    ],
    ids=["memory", "cpu"],
)
def test_a_dropped_limit_named_in_the_warnings_fails_the_create(warning: str) -> None:
    """A sandbox never runs with a limit its caller asked for silently gone."""
    made = asyncio.run(_created(warnings=(warning,)))
    assert isinstance(made, Err)
    assert made.error.code == "unavailable"
    assert "can't enforce" in made.error.message


def test_a_limit_the_applied_host_config_lost_fails_the_create() -> None:
    """The inspect, not just the warnings: the mocked daemon applies neither limit."""
    made = asyncio.run(_created(memory_mb=512))
    assert isinstance(made, Err)
    assert made.error.message.startswith("Docker can't enforce the memory limit")
    made = asyncio.run(_created(cpus=2))
    assert isinstance(made, Err)
    assert made.error.message.startswith("Docker can't enforce the cpu limit")


def test_the_injected_supervisor_is_hashed_against_its_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The binary is build output; one that isn't the pinned build never reaches a container."""
    monkeypatch.setitem(create.BINARIES, "linux-arm64", "0" * 64)
    made = asyncio.run(_created())
    assert isinstance(made, Err)
    assert made.error.message == "the injected supervisor does not match its pinned sha256"


def test_the_pinned_hashes_are_the_ones_the_build_wrote() -> None:
    """docker/supervise/binaries.json is the contract; these constants follow it."""
    pinned = create.BIN_DIR.parents[6] / "docker/supervise/binaries.json"
    assert dict(create.BINARIES) == json.loads(pinned.read_text())


def test_a_check_that_is_not_the_expected_json_fails_the_create() -> None:
    """`supervise --check` prints into the container's stdout: a trust boundary."""
    made = asyncio.run(_created(check_stdout=b"Segmentation fault\n"))
    assert isinstance(made, Err)
    assert made.error.code == "unavailable"


def test_an_image_without_sh_fails_the_create() -> None:
    made = asyncio.run(
        _created(
            check_stdout=b'{"sh":false,"env":true,"bash":false,'
            b'"tar":false,"git":false,"python3":false}'
        )
    )
    assert isinstance(made, Err)
    assert made.error.message == f"image {IMAGE} lacks /bin/sh (docker() needs sh and env)"


def test_an_unreadable_architecture_is_invalid_config() -> None:
    with pytest.raises(ConfigError) as refused:
        asyncio.run(_created(arch="riscv64"))
    assert refused.value.code == "invalid_config"
    assert "riscv64" in refused.value.message


def test_a_record_that_does_not_parse_is_a_typed_failure() -> None:
    """Records are container output: one that isn't a record is unavailable, never a raise."""

    async def main() -> None:
        backend = FakeBackend.scripted()
        engine = DockerEngine(backend)
        async with holding():
            sandbox = adapter(backend, "docker", engine)
            made = await sandbox.create("k", OPEN)
            assert isinstance(made, Ok), made
            assert isinstance(await made.value.exec(["sleep", "100"], OPEN, process_key="p"), Ok)
            engine.bad_record = b'{"key": 7}'
            got = await made.value.terminate("p", OPEN)
        assert isinstance(got, Err)
        assert got.error.code == "unavailable"

    asyncio.run(main())


def test_an_archive_that_is_not_a_tar_is_a_typed_failure() -> None:
    async def main() -> None:
        backend = FakeBackend.scripted()
        engine = DockerEngine(backend)
        async with holding():
            sandbox = adapter(backend, "docker", engine)
            made = await sandbox.create("k", OPEN)
            assert isinstance(made, Ok), made
            assert await made.value.upload("/workspace/a.txt", b"hi", OPEN) == Ok(None)
            engine.corrupt_archive = True
            got = await made.value.download("/workspace/a.txt", OPEN)
        assert isinstance(got, Err)
        assert got.error.code == "unavailable"

    asyncio.run(main())


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


async def _created(
    *,
    warnings: tuple[str, ...] = (),
    cpus: float | None = None,
    memory_mb: int | None = None,
    check_stdout: bytes | None = None,
    arch: str | None = None,
) -> Ok[SandboxSession] | Err[SandboxError]:
    """One create against a mocked daemon a test has broken in one way. The container it
    leaves behind must be gone."""
    backend = FakeBackend.scripted()
    engine = DockerEngine(backend)
    engine.warnings, engine.check_stdout = warnings, check_stdout
    engine.arch = arch or engine.arch
    async with holding():
        made = await adapter(backend, "docker", engine, cpus=cpus, memory_mb=memory_mb).create(
            "k", OPEN
        )
    if isinstance(made, Err):
        assert not any(b.alive for b in backend.boxes.values()), "the container was left behind"
    return made

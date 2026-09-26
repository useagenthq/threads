"""What the dev sandbox's confinement denies. Every case runs the real OS confinement, so it is
skipped where there is none (a Linux host without bubblewrap, or with user namespaces disabled);
CI's Linux job installs bubblewrap so these run there, and they run on macOS as they are.

The two platforms deny different things, and the tests say which:
- both: a home file, the network, another process's environment, an inherited fd.
- Linux only: a private /run and /tmp, and a pid namespace, which come from bwrap's mounts.
- macOS has no mount namespace, so /tmp is denied outright instead of being made private, and
  the sandbox directory keeps its host path.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sandbox_kit import OPEN

from threads.adapters.sandboxes.dev.confine import confinement, probe
from threads.dev import dev_sandbox
from threads.result import Err, Ok
from threads.sandbox.protocol import SandboxError, SandboxSession

_ROOT = tempfile.mkdtemp(prefix="threads-dev-confine-")


def _missing() -> str | None:
    made = confinement(False, None)
    return probe(made.value, _ROOT) if isinstance(made, Ok) else made.error


MISSING = _missing()
REQUIRED = "THREADS_REQUIRE_CONFINEMENT"
"""Set to 1 on a host that must be able to confine (CI's Linux runners, once they install
bubblewrap). Then an unavailable confinement fails instead of skipping: a lane whose denials
quietly stop being asserted looks proven when only half of it ran."""

_WHY = (
    "the dev sandbox has no working OS confinement (bubblewrap on Linux, sandbox-exec on macOS): "
    f"{MISSING}. Install bubblewrap to run these on Linux."
)

confined = pytest.mark.skipif(MISSING is not None, reason=_WHY)
on_linux = pytest.mark.skipif(sys.platform != "linux", reason="bubblewrap's own mounts: Linux only")
on_macos = pytest.mark.skipif(
    sys.platform != "darwin", reason="sandbox-exec's own denials: macOS only"
)


def test_the_confinement_is_available_or_says_by_name_why_not() -> None:
    """The loud half of the skip. It always runs: on a host without a confinement it names what
    is missing in the log, and where one is required it fails rather than letting the suite
    report a quietly smaller test count."""
    if MISSING is None:
        return
    print(f"threads: {_WHY}")
    assert os.environ.get(REQUIRED) != "1", _WHY


def _ok[T](result: Ok[T] | Err[SandboxError]) -> T:
    assert isinstance(result, Ok), f"expected ok, got {result}"
    return result.value


async def _text(stream: AsyncIterator[bytes]) -> str:
    return b"".join([chunk async for chunk in stream]).decode("utf-8", "replace")


async def _box(*, allow_internet: bool = False) -> SandboxSession:
    sandbox = dev_sandbox(root=_ROOT, allow_internet=allow_internet)
    await sandbox.setup()
    return _ok(await sandbox.create(f"op-{uuid.uuid4()}", OPEN))


async def _ran(
    session: SandboxSession, script: str, shell: str = "/bin/sh"
) -> tuple[int, str, str]:
    """The command's exit code, its stdout, and everything it said on either stream."""
    output = _ok(await session.exec([shell, "-c", script], OPEN, process_key=f"k-{uuid.uuid4()}"))
    out, errors = await asyncio.gather(_text(output.stdout), _text(output.stderr))
    return await output.exit_code, out, f"{out}{errors}"


@confined
def test_a_confined_command_runs_with_exactly_the_calls_environment() -> None:
    async def main() -> None:
        session = await _box()
        output = _ok(
            await session.exec(
                ["/usr/bin/env"], OPEN, process_key="k-env", env={"GREETING": "hello"}
            )
        )
        text = await _text(output.stdout)
        lines = text.strip().split("\n") if text.strip() else []
        assert await output.exit_code == 0
        # Names, not values: a CI log masks a value that matches one of its own secrets, so a
        # leak has to be named to be readable at all. bwrap sets PWD from the directory it chdirs
        # into, after the env options, so no flag takes it back; that one name is pinned here
        # rather than waved through, and anything else still fails.
        extra: list[str] = ["PWD"] if sys.platform == "linux" else []
        assert sorted(line.split("=")[0] for line in lines) == sorted(["GREETING", *extra])
        assert "GREETING=hello" in lines
        if sys.platform == "linux":
            assert "PWD=/workspace" in lines

    asyncio.run(main())


@confined
def test_a_confined_command_sees_no_variable_of_the_hosts_own_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What invariant 4 needs: a value the host holds cannot reach a command, whatever the
    platform does about PWD. bwrap spawns with an empty env and --clearenv; sandbox-exec spawns
    with exactly the call's env, which is the same promise by a different route."""
    monkeypatch.setenv("THREADS_DEV_HOST_SECRET", "a credential")

    async def main() -> None:
        session = await _box()
        _code, out, said = await _ran(session, "env; echo done")
        assert "done" in out
        assert "THREADS_DEV_HOST_SECRET" not in said
        assert "a credential" not in said

    asyncio.run(main())


@confined
def test_a_confined_command_writes_only_inside_its_workspace_and_the_host_is_unchanged() -> None:
    async def main() -> None:
        session = await _box()
        code, _, _said = await _ran(session, "echo made > made.txt")
        assert code == 0
        assert _ok(await session.download("/workspace/made.txt", OPEN)) == b"made\n"

        # The promise is that the host is unchanged, not that every write fails. bwrap gives the
        # command a private tmpfs root, so a write outside /workspace can succeed inside and go
        # away with the sandbox; sandbox-exec has no root to replace and refuses it instead.
        # This asserts the part that has to hold on either platform: the dev root is writable by
        # this user on the host, so a file appearing there would be a real escape.
        outside = Path(_ROOT) / "threads-escape.txt"
        _code, out, _said = await _ran(session, f'echo x > "{outside}" 2>&1; echo done')
        assert "done" in out
        assert not outside.exists()
        assert not Path("/threads-escape.txt").exists()

    asyncio.run(main())


@confined
def test_a_confined_command_cannot_read_a_planted_home_file() -> None:
    planted = Path.home() / ".threads-dev-home-secret"
    planted.write_text("a credential")
    try:

        async def main() -> None:
            session = await _box()
            code, _, said = await _ran(session, f'cat "{planted}" 2>&1')
            assert code != 0
            assert "a credential" not in said

        asyncio.run(main())
    finally:
        planted.unlink(missing_ok=True)


@confined
def test_a_confined_command_has_no_network_and_allow_internet_is_what_opens_it() -> None:
    async def main() -> None:
        server = await asyncio.start_server(_close, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            # bash's /dev/tcp, because dash has none: the open case proves the probe works.
            script = f"exec 3<>/dev/tcp/127.0.0.1/{port} 2>/dev/null && echo open || echo closed"
            _, denied, _ = await _ran(await _box(), script, "/bin/bash")
            assert denied.strip() == "closed"
            _, opened, _ = await _ran(await _box(allow_internet=True), script, "/bin/bash")
            assert opened.strip() == "open"

    asyncio.run(main())


async def _close(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.close()


@confined
def test_a_confined_command_cannot_read_another_processs_environment() -> None:
    helper = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"],
        env={"THREADS_DEV_SECRET": "a credential"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:

        async def main() -> None:
            session = await _box()
            _, _out, said = await _ran(
                session,
                f"cat /proc/{helper.pid}/environ 2>&1; ps -p {helper.pid} -wwE 2>&1",
            )
            assert "a credential" not in said

        asyncio.run(main())
    finally:
        helper.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            helper.wait(timeout=5)


@confined
def test_a_confined_command_inherits_no_file_descriptor_beyond_the_three_pipes() -> None:
    async def main() -> None:
        session = await _box()
        _, _out, said = await _ran(session, "cat <&3 2>&1; echo done")
        assert "done" in said
        assert "a credential" not in said

    asyncio.run(main())


@confined
@on_linux
def test_bubblewraps_run_and_tmp_are_private_and_the_pid_namespace_hides_the_host() -> None:
    async def main() -> None:
        session = await _box()
        _, run, _r = await _ran(session, "ls -A /run | wc -l")
        assert run.strip() == "0"
        _, temp, _t = await _ran(session, "ls -A /tmp | wc -l")
        assert temp.strip() == "0"
        # PID 1 of the namespace is the wrapper itself, so the host's processes are not there.
        _, ours, _p = await _ran(session, "ls -d /proc/[0-9]* | wc -l")
        assert int(ours.strip()) < 10  # noqa: PLR2004 - a handful, not the host's hundreds

    asyncio.run(main())


@confined
@on_macos
def test_sandbox_exec_denies_the_hosts_temp_directory_outside_the_sandbox() -> None:
    outside = Path(_ROOT) / "outside.txt"
    outside.write_text("a credential")

    async def main() -> None:
        session = await _box()
        code, _, said = await _ran(session, f'cat "{outside}" 2>&1')
        assert code != 0
        assert "a credential" not in said
        assert os.path.exists(outside)

    asyncio.run(main())

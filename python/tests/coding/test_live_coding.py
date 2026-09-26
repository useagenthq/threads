"""Live gate for the preset's sandbox: a real container on the local daemon, so what is proven
here can't be faked — real git in the default image, the cgroup limits the daemon really applied,
and an environment the container cannot see.

The sandbox is the preset's own: `coding_agent().definition.sandbox` is the `docker()` the preset
built, limits and all. Gated on THREADS_LIVE=1 and the `live` marker, like test_live_docker.py; a
daemon that isn't there fails the gate rather than skipping it, because the workflow that runs this
job installs one.

    THREADS_LIVE=1 uv run pytest -m live tests/coding/test_live_coding.py
"""

import asyncio
import os
import uuid

import pytest
from loop_kit import held
from sandbox_kit import OPEN

from threads.adapters.sandboxes.posix import collect
from threads.agents.setup import SetsUp
from threads.coding import CODING_INSTRUCTIONS, coding_agent
from threads.result import Ok
from threads.sandbox import Sandbox, SandboxSession

pytestmark = pytest.mark.live

PATH = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
# The two commands CODING_INSTRUCTIONS tells the model to run, taken from the constant.
BASELINE = (
    "git init -q; git add -A && git -c user.name=threads -c user.email=threads@localhost "
    "commit -q --no-verify --allow-empty -m threads-baseline && git tag -f threads-baseline"
)
DIFF = "git add -A && git add -A --renormalize && git diff --cached --binary threads-baseline"
# A repository with a commit and a pre-commit hook that always fails, and a plain directory: the
# baseline is recorded either way.
EXISTING = " && ".join(
    (
        "git init -q",
        "git add -A",
        "git -c user.name=t -c user.email=t@l commit -q -m first",
        r"printf '#!/bin/sh\nexit 1\n' > .git/hooks/pre-commit",
        "chmod +x .git/hooks/pre-commit",
    )
)


def _requires_live() -> None:
    if os.environ.get("THREADS_LIVE") != "1":
        pytest.skip("live gate: set THREADS_LIVE=1 with a running Docker daemon")


def _preset_sandbox() -> Sandbox:
    """The docker() the preset built, limits and all, set up on the host."""
    sandbox = coding_agent().definition.sandbox
    assert sandbox is not None, "the preset sets a sandbox"
    assert isinstance(sandbox, SetsUp), "docker() resolves its socket in setup"
    asyncio.run(sandbox.setup())
    return sandbox


async def _sh(session: SandboxSession, script: str, key: str) -> tuple[int, str, str]:
    ran = await session.exec(["/bin/sh", "-c", script], OPEN, process_key=key, env=PATH)
    assert isinstance(ran, Ok), ran
    code, out, err = await collect(ran.value)
    return code, out.decode(), err.decode()


def test_the_commands_are_the_ones_the_instructions_name() -> None:
    assert BASELINE in CODING_INSTRUCTIONS
    assert DIFF in CODING_INSTRUCTIONS


def test_the_diff_carries_an_edit_a_new_file_and_a_binary() -> None:
    _requires_live()
    sandbox = _preset_sandbox()

    async def main() -> None:
        for before in ("true", EXISTING):
            made = await sandbox.create(str(uuid.uuid4()), OPEN)
            assert isinstance(made, Ok), made
            session = made.value
            try:
                assert await session.upload("/workspace/src/app.ts", b"old\n", OPEN) == Ok(None)
                assert (await _sh(session, before, "k1"))[0] == 0
                assert (await _sh(session, BASELINE, "k2"))[0] == 0
                # The edit is exactly as long as what it replaces, on purpose: an uploaded file
                # lands with mtime 0, so that is the case git's stat cache hides and the
                # instructions carry --renormalize for.
                assert await session.upload("/workspace/src/app.ts", b"new\n", OPEN) == Ok(None)
                assert await session.upload("/workspace/src/new.ts", b"added\n", OPEN) == Ok(None)
                made_binary = await _sh(session, r"printf '\000\001\002\377' > logo.bin", "k3")
                assert made_binary[0] == 0, made_binary
                code, diff, _ = await _sh(session, DIFF, "k4")
                assert code == 0
                assert "-old" in diff
                assert "+new" in diff
                assert "new file mode" in diff
                assert "src/new.ts" in diff
                assert "GIT binary patch" in diff
            finally:
                assert await session.close(OPEN) == Ok(None)

    asyncio.run(held(main()))


def test_the_image_carries_git_the_limits_bind_and_the_hosts_key_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _requires_live()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    sandbox = _preset_sandbox()

    async def main() -> None:
        made = await sandbox.create(str(uuid.uuid4()), OPEN)
        assert isinstance(made, Ok), made
        session = made.value
        try:
            assert "git version" in (await _sh(session, "git --version", "g1"))[1]
            # The limits coding_agent() asks for, as the daemon applied them (cgroup v2).
            limits = await _sh(
                session,
                "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max /sys/fs/cgroup/pids.max",
                "g2",
            )
            assert limits[1].split("\n")[:3] == ["4294967296", "200000 100000", "1024"]
            # Invariant 4: exactly the call's environment, and PID 1's is unreadable. Names, not
            # values: a CI log masks a value it knows, so a leak has to be named to be readable.
            ran = await session.exec(["printenv"], OPEN, process_key="g3", env={"ONLY": "this"})
            assert isinstance(ran, Ok), ran
            assert await collect(ran.value) == (0, b"ONLY=this\n", b"")
            sealed = await _sh(session, "cat /proc/1/environ 2>&1; true", "g4")
            assert "Permission denied" in sealed[1]
            assert "ANTHROPIC_API_KEY" not in sealed[1]
        finally:
            assert await session.close(OPEN) == Ok(None)

    asyncio.run(held(main()))

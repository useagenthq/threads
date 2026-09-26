"""`dev_sandbox()`: what it declares, the directory one operation key owns, and the host file
operations that never follow a symlink. Running a command is test_dev_confine.py."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sandbox_kit import OPEN, KitContext

from threads.adapters.sandboxes.dev import DevSandbox
from threads.adapters.sandboxes.dev.confine import DEFAULT_TOOL, confinement
from threads.agents.config import ConfigError
from threads.dev import dev_sandbox
from threads.loop.model import Found, NotFound
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    LookupSupport,
    SandboxError,
    SandboxInfo,
    SandboxSession,
    Trees,
)
from threads.sandbox.tree.tree import Tree, TreeFile
from threads.sandbox.trees import HashOnly, read_tree
from threads.store.artifacts import MemoryArtifacts

STALE = KitContext(live=False)
STUB = "/usr/bin/true"
"""Stands in for the confinement in this file. These cases exercise the ledger, the host path
walk and the tree, never a confined command, so a trivial program that the probe accepts lets
them run on any host. The confinement itself is test_dev_confine.py, which skips loudly."""

EXECUTABLE = 0o755
"""The mode an import keeps; a setuid bit above it never survives."""


def _failed(result: Ok[object] | Err[SandboxError]) -> str:
    assert isinstance(result, Err), f"expected a failure, got {result}"
    return result.error.code


def _ok[T](result: Ok[T] | Err[object]) -> T:
    assert isinstance(result, Ok), f"expected ok, got {result}"
    return result.value


def _trees(session: SandboxSession) -> Trees:
    assert isinstance(session, Trees)
    return session


def _shape(tree: Tree) -> list[tuple[str, str]]:
    return [(e.path, e.kind) for e in tree.entries]


def _mode(tree: Tree, path: str) -> int:
    for entry in tree.entries:
        if entry.path == path and isinstance(entry, TreeFile):
            return entry.mode
    raise AssertionError(f"no file {path}")


async def _opened(sandbox: DevSandbox, key: str = "op-1") -> SandboxSession:
    return _ok(await sandbox.create(key, OPEN))


def test_declarations_have_no_snapshots_a_final_lookup_and_denied_egress(tmp_path: Path) -> None:
    assert dev_sandbox(root=str(tmp_path), tool=STUB).info == SandboxInfo(
        provider="dev",
        egress="enforced",
        capture_classes=(),
        browser="none",
        desktop="none",
        lookup=LookupSupport(create="final", snapshot="none"),
        termination="unconfirmed",
    )


def test_allow_internet_declares_egress_unenforced(tmp_path: Path) -> None:
    assert (
        dev_sandbox(root=str(tmp_path), allow_internet=True, tool=STUB).info.egress == "unenforced"
    )


def test_the_confinement_defaults_to_the_platforms_and_one_that_cannot_run_is_refused(
    tmp_path: Path,
) -> None:
    made = confinement(False, None)
    assert isinstance(made, Ok)
    assert made.value.tool == DEFAULT_TOOL[sys.platform]

    sandbox = dev_sandbox(root=str(tmp_path), tool="threads-no-such-confinement")
    with pytest.raises(ConfigError) as raised:
        asyncio.run(sandbox.setup())
    assert raised.value.code == "capability_missing"
    assert "threads-no-such-confinement" in raised.value.message


def test_the_sandbox_directories_live_in_threads_dev_in_the_hosts_temp_directory() -> None:
    async def main() -> None:
        # /usr/bin/true stands in for the confinement, so this runs wherever the tests do.
        sandbox = dev_sandbox(tool=STUB)
        session = await _opened(sandbox, "op-default-root")
        try:
            default = Path(os.path.realpath(tempfile.gettempdir())) / "threads-dev"
            assert (default / session.id).is_dir()
        finally:
            await session.close(OPEN)

    asyncio.run(main())


def test_restore_release_and_snapshot_refuse_there_are_no_snapshots(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        assert _failed(await sandbox.restore("snap", "hash", "op", OPEN)) == "snapshot_missing"
        assert _failed(await sandbox.release("snap", OPEN)) == "unavailable"
        session = await _opened(sandbox)
        assert _failed(await session.snapshot("op-snap", OPEN)) == "unavailable"
        # Unconfirmed: an in-doubt call parks instead of being retried.
        assert await session.terminate("key", OPEN) == Ok("unknown")

    asyncio.run(main())


def test_create_makes_the_keys_directory_and_lookup_is_final_on_it(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        assert session.id.startswith("threads-dev-")
        assert (tmp_path / session.id).is_dir()

        found = await sandbox.lookup("op-1", OPEN)
        assert isinstance(found, Ok)
        assert isinstance(found.value, Found)
        # Nothing else can have made that directory, so absence is proof.
        absent = await sandbox.lookup("op-2", OPEN)
        assert isinstance(absent, Ok)
        assert isinstance(absent.value, NotFound)

    asyncio.run(main())


def test_a_second_create_of_the_same_key_answers_the_same_directory(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        first = await _opened(sandbox)
        again = await _opened(sandbox)
        assert again.id == first.id
        assert len(list(tmp_path.iterdir())) == 1

    asyncio.run(main())


def test_attach_finds_a_live_sandbox_and_refuses_a_ref_that_is_not_one(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        assert _ok(await sandbox.attach(session.id, OPEN)).id == session.id
        assert _failed(await sandbox.attach("../escape", OPEN)) == "resource_unknown"
        assert _failed(await sandbox.attach("threads-dev-gone", OPEN)) == "not_found"

    asyncio.run(main())


def test_close_removes_the_directory(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        assert await session.close(OPEN) == Ok(None)
        assert list(tmp_path.iterdir()) == []

    asyncio.run(main())


def test_a_stale_owner_creates_nothing_and_writes_nothing(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        assert _failed(await sandbox.create("op-1", STALE)) == "stale_epoch"
        assert list(tmp_path.iterdir()) == []

        session = await _opened(sandbox)
        assert _failed(await session.upload("/workspace/a.txt", b"x", STALE)) == "stale_epoch"
        assert list((tmp_path / session.id).iterdir()) == []
        assert _failed(await session.download("/workspace/a.txt", STALE)) == "stale_epoch"

    asyncio.run(main())


def test_a_stale_writer_spawns_nothing(tmp_path: Path) -> None:
    async def main() -> None:
        # /usr/bin/true stands in for the confinement: the fence is refused before any spawn, so
        # this runs wherever the tests do and nothing is ever confined.
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        spawned = await session.exec(
            ["/bin/sh", "-c", "echo x > spawned.txt"], STALE, process_key="k-stale"
        )
        assert _failed(spawned) == "stale_epoch"
        assert list((tmp_path / session.id).iterdir()) == []

    asyncio.run(main())


def test_a_symlink_chain_never_reaches_a_file_outside_the_root(tmp_path: Path) -> None:
    async def main() -> None:
        (tmp_path / "outside.txt").write_text("a credential")
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        at = tmp_path / session.id
        # The rev-7 chain: `a/b -> ..` then `c -> a/b/..` leaves the root one lexical step at a
        # time, so a per-component check is the only thing that catches it.
        (at / "a").mkdir()
        os.symlink("..", at / "a" / "b")
        os.symlink("a/b/..", at / "c")

        read = await session.download("/workspace/c/outside.txt", OPEN)
        assert _failed(read) == "invalid_path"
        planted = await session.upload("/workspace/c/planted.txt", b"x", OPEN)
        assert _failed(planted) == "invalid_path"
        assert not (tmp_path / "planted.txt").exists()

        tree = _ok(await read_tree(_trees(session), OPEN, HashOnly))
        assert _shape(tree) == [("a", "dir"), ("a/b", "symlink"), ("c", "symlink")]

    asyncio.run(main())


def test_a_cwd_reached_through_a_symlink_is_refused(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        os.symlink("..", tmp_path / session.id / "up")
        ran = await session.exec(
            ["/bin/sh", "-c", "pwd"], OPEN, process_key="k-cwd", cwd="/workspace/up"
        )
        assert _failed(ran) == "invalid_path"

    asyncio.run(main())


def test_a_path_outside_workspace_is_refused(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        assert _failed(await session.download("/etc/passwd", OPEN)) == "invalid_path"
        outside = await session.upload("/tmp/planted", b"x", OPEN)  # noqa: S108 - the point
        assert _failed(outside) == "invalid_path"

    asyncio.run(main())


def test_a_file_its_mode_and_a_symlink_survive_an_export_and_an_import(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        at = tmp_path / session.id
        (at / "sub").mkdir()
        (at / "sub" / "run.sh").write_text("#!/bin/sh\n")
        (at / "sub" / "run.sh").chmod(0o755)
        os.symlink("sub/run.sh", at / "link")

        exported = _ok(await read_tree(_trees(session), OPEN, MemoryArtifacts().sink))
        assert _shape(exported) == [("link", "symlink"), ("sub", "dir"), ("sub/run.sh", "file")]

        other = await _opened(sandbox, "op-2")
        archive = _ok(await _trees(session).export_tree(OPEN))
        assert await _trees(other).import_tree(archive.stdout, OPEN) == Ok(None)
        back = _ok(await read_tree(_trees(other), OPEN, HashOnly))
        assert _shape(back) == _shape(exported)
        assert _mode(back, "sub/run.sh") == EXECUTABLE

    asyncio.run(main())


def test_a_setuid_bit_never_survives_an_import(tmp_path: Path) -> None:
    async def main() -> None:
        sandbox = dev_sandbox(root=str(tmp_path), tool=STUB)
        session = await _opened(sandbox)
        suid = tmp_path / session.id / "suid"
        suid.write_text("x")
        suid.chmod(0o4755)

        other = await _opened(sandbox, "op-2")
        archive = _ok(await _trees(session).export_tree(OPEN))
        assert await _trees(other).import_tree(archive.stdout, OPEN) == Ok(None)
        back = _ok(await read_tree(_trees(other), OPEN, HashOnly))
        assert _mode(back, "suid") == EXECUTABLE

    asyncio.run(main())

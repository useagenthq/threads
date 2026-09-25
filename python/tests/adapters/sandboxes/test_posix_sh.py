"""The sandbox-side scripts run in a real `sh`, as a provider runs them, instead of through a
fake that synthesizes their output."""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

import pytest
from sandbox_kit import OPEN
from tree_contract import EXECUTABLE, sample_tree

from threads.adapters.sandboxes.posix import EXPORT_TREE, IMPORT_TREE, WORKSPACE, wrap
from threads.result import Err, Ok
from threads.sandbox.protocol import ExecOutput, SandboxContext, SandboxError
from threads.sandbox.tree.tree import Tree
from threads.sandbox.trees import Misplaced, place_tree, tree_hash
from threads.store.artifacts import MemoryArtifacts

NOT_FOUND = 127


def _printed_env(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    # `probe` is outside the libc default /bin:/usr/bin, like python3 in /usr/local/bin on
    # python:* images; it is `env`, so it prints exactly the environment it got.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "probe").symlink_to("/usr/bin/env")
    # The provider applies the env it is given over its own, as E2B, Modal and Daytona do. Its
    # own env may even hold the wrapper's carrier name, or a name no shell can unset; neither
    # may reach the command.
    inherited = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SECRET": "host-only",
        "__t_PATH": "leak",
        "BAD NAME": "secret",
    }
    wrapped = wrap(["probe"], env)
    provider = {**inherited, **wrapped.env}
    done = subprocess.run(wrapped.argv, env=provider, capture_output=True, check=True)  # noqa: S603
    lines = done.stdout.decode().splitlines()
    return {name: value for name, _, value in (line.partition("=") for line in lines)}


def test_argv0_resolves_on_the_provider_path_and_runs_with_exactly_the_tool_env(
    tmp_path: Path,
) -> None:
    assert _printed_env(tmp_path, {"K": "v w"}) == {"K": "v w"}


def test_names_a_shell_sets_for_itself_still_carry_the_tool_values(tmp_path: Path) -> None:
    env = {"PWD": "caller", "SHLVL": "caller", "IFS": ":", "_": "u", "OLDPWD": "o"}
    assert _printed_env(tmp_path, env) == env


def test_a_tool_path_does_not_change_where_argv0_is_found(tmp_path: Path) -> None:
    # `probe` is only on the provider PATH; the command still runs with the tool's PATH.
    assert _printed_env(tmp_path, {"PATH": "/nowhere"}) == {"PATH": "/nowhere"}


@pytest.mark.parametrize("name", ["no-such-threads-command", "export"])
def test_a_missing_command_or_a_builtin_exits_127_and_says_so(name: str) -> None:
    done = subprocess.run(  # noqa: S603
        wrap([name], {}).argv, env={"PATH": "/usr/bin:/bin"}, capture_output=True, check=False
    )
    assert done.returncode == NOT_FOUND
    assert f"threads: command not found: {name}".encode() in done.stderr


# A fixed tree: nested dirs, a non-ASCII name, a space, an executable, an empty file. Its hash
# was pinned by the retired in-sandbox manifest script (find, stat, sha256sum), and
# typescript/packages/core/test/sandbox/remote/scripts-sh.test.ts pins it too: trees hash as
# manifests did, in both languages.
TREE = (
    ("a.txt", b"hello\n", 0o644),
    ("dir/sub/run.sh", b"#!/bin/sh\necho hi\n", 0o755),
    ("dir/with space.md", b"x", 0o600),
    ("héllo/ünï.txt", b"unicode", 0o644),
    ("empty", b"", 0o644),
)
TREE_HASH = "5002ad0ef59bdacdec8326269f3818c29b9f57ec31ff8c1973451a54a5b2a60f"
# macOS tar would add AppleDouble `._` entries for extended metadata.
_ENV = {"PATH": "/usr/bin:/bin", "COPYFILE_DISABLE": "1"}


class _Shell:
    """The kit's tree scripts run by a real `sh` over `root` as /workspace (bsdtar on macOS)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _here(self, script: str) -> str:
        return script.replace(WORKSPACE, str(self._root))

    async def export_tree(self, context: SandboxContext) -> Ok[ExecOutput] | Err[SandboxError]:
        del context
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", self._here(EXPORT_TREE), env=_ENV, stdout=-1, stderr=-1
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        return Ok(ExecOutput(proc.wait(), _chunks(proc.stdout), _chunks(proc.stderr)))

    async def import_tree(
        self, tar: AsyncIterable[bytes], context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        del context
        upload = self._root.parent / f"{self._root.name}.tar"
        upload.write_bytes(b"".join([chunk async for chunk in tar]))
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["/bin/sh", "-c", self._here(IMPORT_TREE), "threads", str(upload)],
            env=_ENV,
            capture_output=True,
            check=False,
        )
        assert not upload.exists(), "the import left its archive behind"
        if done.returncode:
            return Err(SandboxError("unavailable", done.stderr.decode()))
        return Ok(None)


async def _chunks(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    while chunk := await stream.read(65536):
        yield chunk


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_an_export_hashes_a_tree_as_the_retired_manifest_script_did(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for path, body, mode in TREE:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(body)
        (root / path).chmod(mode)
    assert asyncio.run(tree_hash(_Shell(root), OPEN)) == Ok(TREE_HASH)


def test_place_tree_keeps_modes_and_symlinks_and_masks_setuid(tmp_path: Path) -> None:
    root = _root(tmp_path)
    artifacts = MemoryArtifacts()
    placed = asyncio.run(place_tree(_Shell(root), sample_tree(artifacts), artifacts.get, OPEN))
    assert placed == Ok(None)
    assert (root / "bin/run").stat().st_mode & 0o7777 == EXECUTABLE
    assert (root / "bin/su").stat().st_mode & 0o7777 == EXECUTABLE
    assert os.readlink(root / "link") == "bin/run"


def test_place_tree_refuses_a_workspace_that_is_not_empty(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "left").write_bytes(b"x")
    empty = Tree(tree_version=1, entries=[])
    placed = asyncio.run(place_tree(_Shell(root), empty, MemoryArtifacts().get, OPEN))
    assert isinstance(placed, Err)
    assert isinstance(placed.error, Misplaced)
    assert placed.error.code == "workspace_not_empty"

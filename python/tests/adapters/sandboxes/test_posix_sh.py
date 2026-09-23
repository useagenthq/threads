"""The sandbox-side scripts run in a real `sh`, as a provider runs them, instead of through a
fake that synthesizes their output."""

import hashlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from threads.adapters.sandboxes.posix import (
    MANIFEST,
    WORKSPACE,
    parse_manifest,
    wrap,
)
from threads.result import Ok
from threads.sandbox.manifest import ManifestEntry, in_order, manifest_hash

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


# The tree typescript/packages/core/test/sandbox/remote/scripts-sh.test.ts pins too, so both
# languages agree on the hash: nested dirs, a non-ASCII name, a space, an executable, an empty
# file.
TREE = (
    ("a.txt", b"hello\n", 0o644),
    ("dir/sub/run.sh", b"#!/bin/sh\necho hi\n", 0o755),
    ("dir/with space.md", b"x", 0o600),
    ("héllo/ünï.txt", b"unicode", 0o644),
    ("empty", b"", 0o644),
)
TREE_HASH = "5002ad0ef59bdacdec8326269f3818c29b9f57ec31ff8c1973451a54a5b2a60f"


@pytest.mark.skipif(sys.platform != "linux", reason="needs GNU `stat -c`")
def test_the_manifest_lists_the_tree_exactly_as_the_host_sees_it(tmp_path: Path) -> None:
    for path, body, mode in TREE:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(body)
        (tmp_path / path).chmod(mode)
    script = MANIFEST.replace(f"cd {WORKSPACE}", f"cd {shlex.quote(str(tmp_path))}")
    done = subprocess.run(["/bin/sh", "-c", script], capture_output=True, check=True)  # noqa: S603
    expected = in_order(
        ManifestEntry(path=p, mode=m, size=len(b), sha256=hashlib.sha256(b).hexdigest())
        for p, b, m in TREE
    )
    assert parse_manifest(done.stdout) == Ok(expected)
    assert manifest_hash(expected) == TREE_HASH


def test_an_empty_mode_field_from_bsd_stat_is_refused() -> None:
    assert not isinstance(parse_manifest(b"a\0\x001\0" + b"a" * 64 + b"\0"), Ok)

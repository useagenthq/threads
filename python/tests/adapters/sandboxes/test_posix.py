"""The sandbox-side scripts every adapter shares: the manifest parser is a trust boundary (the
guest writes its input), and the exec wrapper's argv carries env names, never values."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sandbox_kit import OPEN

from threads.adapters.sandboxes.posix import WRAPPER, parse_manifest, run, stdin_path, wrap
from threads.result import Err, Ok
from threads.sandbox.manifest import ManifestEntry, in_order, manifest_hash, manifest_of
from threads.sandbox.protocol import ExecOutput, SandboxContext, SandboxError

_DIGEST = "a" * 64
VECTOR = (
    Path(__file__).resolve().parents[4] / "spec" / "conformance" / "vectors" / "manifest-order.json"
)


def _nul(entries: list[ManifestEntry]) -> bytes:
    return b"".join(
        f"{e['path']}\0{e['mode']:o}\0{e['size']}\0{e['sha256']}\0".encode() for e in entries
    )


_PATHS = st.text(st.characters(exclude_categories=["Cs"], exclude_characters="\0"), min_size=1)


def _entry(path: str, mode: int, size: int) -> ManifestEntry:
    return ManifestEntry(path=path, mode=mode, size=size, sha256=_DIGEST)


_ENTRIES = st.lists(
    st.builds(_entry, _PATHS, st.integers(0, 0o7777), st.integers(0, 2**40)),
    unique_by=lambda e: e["path"],
)


@given(_ENTRIES)
def test_the_manifest_output_round_trips_in_utf16_order(entries: list[ManifestEntry]) -> None:
    assert parse_manifest(_nul(entries)) == Ok(in_order(entries))


def test_paths_order_by_utf16_code_units_like_jcs_keys() -> None:
    """U+1F600 (a surrogate pair) sorts before U+E000 in UTF-16, after it by code
    point."""
    tree = {"": b"", "\U0001f600": b"", "a": b""}
    assert [e["path"] for e in manifest_of(tree)] == ["a", "\U0001f600", ""]
    parsed = parse_manifest(_nul(manifest_of(tree)[::-1]))
    assert isinstance(parsed, Ok)
    assert [e["path"] for e in parsed.value] == ["a", "\U0001f600", ""]


def test_the_parsed_manifest_matches_the_shared_vector() -> None:
    """What the in-sandbox script prints for the vector's tree, in any order, parses to the
    vector's paths and hash."""
    vector = json.loads(VECTOR.read_text(encoding="utf-8"))
    tree = {f["path"]: f["data"].encode() for f in vector["files"]}
    parsed = parse_manifest(_nul(manifest_of(tree)[::-1]))
    assert isinstance(parsed, Ok)
    assert [e["path"] for e in parsed.value] == vector["manifest_paths"]
    assert manifest_hash(parsed.value) == vector["manifest_hash"]


@pytest.mark.parametrize(
    "out",
    [
        b"a\x00644\x001\x00",  # a field short
        b"a\x00644\x001\x00" + _DIGEST.encode(),  # not NUL-terminated
        b"a\x00999\x001\x00" + _DIGEST.encode() + b"\x00",  # not octal
        b"a\x00644\x00-1\x00" + _DIGEST.encode() + b"\x00",  # negative size
        b"a\x00644\x001\x00" + b"A" * 64 + b"\x00",  # not a lowercase digest
        b"\x00644\x001\x00" + _DIGEST.encode() + b"\x00",  # empty path
        b"\xff\x00644\x001\x00" + _DIGEST.encode() + b"\x00",  # not UTF-8
    ],
)
def test_a_malformed_manifest_is_refused(out: bytes) -> None:
    got = parse_manifest(out)
    assert isinstance(got, Err)
    assert got.error.code == "unavailable"


def test_the_wrapper_argv_names_env_but_never_carries_values() -> None:
    wrapped = wrap(["printenv", "X"], {"B": "secret-b", "A": "secret-a"}, stdin_path("key"))
    assert wrapped.argv[:5] == ("/bin/sh", "-c", WRAPPER, "threads", "A B")
    assert wrapped.argv[5] == stdin_path("key")
    assert wrapped.argv[6:] == ("printenv", "X")
    assert not any("secret" in arg for arg in wrapped.argv)
    # The values travel through the provider API under carrier names the wrapper shell never
    # interprets itself.
    assert wrapped.env == {"__t_v_A": "secret-a", "__t_v_B": "secret-b"}
    with pytest.raises(ValueError, match="not a shell name"):
        wrap(["x"], {"BAD NAME": "v"})
    with pytest.raises(ValueError, match="needs a command"):
        wrap([], {})
    with pytest.raises(ValueError, match="reserved"):
        wrap(["x"], {"__t_PATH": "/nowhere"})


class _Recording:
    """A provider session that records what reached it."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def start(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        context: SandboxContext,
        process_key: str | None = None,
    ) -> Ok[ExecOutput] | Err[SandboxError]:
        self.sent.append("start")
        raise AssertionError("a caller bug must not start")

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        self.sent.append(path)
        return Ok(None)


@pytest.mark.parametrize(
    ("command", "env", "error"),
    [
        ([], {}, "needs a command"),
        (["printenv"], {"BAD NAME": "v"}, "not a shell name"),
        (["printenv"], {"__t_PATH": "/nowhere"}, "reserved"),
    ],
)
def test_a_caller_bug_raises_and_sends_nothing(
    command: list[str], env: dict[str, str], error: str
) -> None:
    session = _Recording()
    bug = run(session, command, OPEN, process_key="k", cwd="/workspace", env=env, stdin=b"x")
    with pytest.raises(ValueError, match=error):
        asyncio.run(bug)
    assert session.sent == []


class _Mutating:
    """A provider whose stdin upload is where a caller changes the inputs it passed."""

    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self.command, self.env = command, env
        self.started: tuple[Sequence[str], Mapping[str, str]] | None = None

    async def start(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        context: SandboxContext,
        process_key: str | None = None,
    ) -> Ok[ExecOutput] | Err[SandboxError]:
        self.started = (argv, env)
        return Err(SandboxError("unavailable", "test: stop here"))

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        self.command.append("--changed")
        self.env["__t_keep"] = "x"
        return Ok(None)


def test_exec_sends_the_inputs_it_checked_even_if_the_caller_changes_them() -> None:
    command, env = ["printenv"], {"K": "v"}
    session = _Mutating(command, env)
    asyncio.run(run(session, command, OPEN, process_key="k", cwd="/", env=env, stdin=b"x"))
    assert session.started is not None
    argv, sent_env = session.started
    assert argv[-1] == "printenv"
    assert dict(sent_env) == {"__t_v_K": "v"}

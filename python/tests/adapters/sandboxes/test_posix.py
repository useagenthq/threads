"""The sandbox-side scripts every adapter shares: the manifest parser is a trust boundary (the
guest writes its input), and the exec wrapper's argv carries env names, never values."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from threads.adapters.sandboxes.posix import WRAPPER, parse_manifest, stdin_path, wrap
from threads.result import Err, Ok
from threads.sandbox.manifest import ManifestEntry, in_order, manifest_of

_DIGEST = "a" * 64


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
    argv = wrap(["printenv", "X"], {"B": "secret-b", "A": "secret-a"}, stdin_path("key"))
    assert argv[:5] == ("/bin/sh", "-c", WRAPPER, "threads", "A B")
    assert argv[5] == stdin_path("key")
    assert argv[6:] == ("printenv", "X")
    assert not any("secret" in arg for arg in argv)
    with pytest.raises(ValueError, match="variable name"):
        wrap(["x"], {"BAD NAME": "v"})
    with pytest.raises(ValueError, match="needs a command"):
        wrap([], {})

"""The captured file-tree manifest: path, mode, size and sha256 per file, and
its canonical hash, which a restore verifies."""

from collections.abc import Iterable, Mapping
from typing import TypedDict

from pydantic import ConfigDict, JsonValue, with_config

from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize, utf16_key
from threads.result import Err, Ok


@with_config(ConfigDict(extra="forbid", strict=True))
class ManifestEntry(TypedDict):
    path: str
    mode: int
    size: int
    sha256: str


WORKSPACE = "/workspace/"


def in_order(entries: Iterable[ManifestEntry]) -> list[ManifestEntry]:
    """Entries by path in UTF-16 code units (spec/schema/README.md, Snapshot manifest), as
    RFC 8785 orders keys: U+1F600 sorts before U+E000."""
    return sorted(entries, key=lambda e: utf16_key(e["path"]))


def manifest_of(files: Mapping[str, bytes]) -> list[ManifestEntry]:
    """The manifest of an in-memory tree: path relative to /workspace, mode 0644 for every
    file."""
    return in_order(
        ManifestEntry(
            path=path.removeprefix(WORKSPACE), mode=0o644, size=len(data), sha256=sha256_hex(data)
        )
        for path, data in files.items()
    )


def manifest_hash(manifest: list[ManifestEntry]) -> str:
    """The manifest's canonical hash (Domain A)."""
    value: list[JsonValue] = [
        {"path": e["path"], "mode": e["mode"], "size": e["size"], "sha256": e["sha256"]}
        for e in manifest
    ]
    match canonicalize(value):
        case Ok(value=text):
            return sha256_hex(text.encode("utf-8"))
        case Err(error=reason):
            raise ValueError(reason)

"""A real tree archived by the host's `tar -cf - -C dir .` (GNU tar on Linux, bsdtar on macOS)
reads as the tree on disk. The hash of a tree exported this way equals the retired manifest
script's (tests/adapters/sandboxes/test_posix_sh.py pins that script's hash)."""

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tar_kit import chunked

from threads.result import Ok
from threads.sandbox.tree.tar import StoredTree, store_tar
from threads.sandbox.tree.tree import TreeDir, TreeFile, TreeSymlink
from threads.store.artifacts import MemoryArtifacts

LONG = "déjà vu/" + "n" * 120
FILES = (
    ("a", b"hello", 0o755),
    ("empty", b"", 0o600),
    ("sub/日本.txt", b"nihon", 0o644),
    (f"sub/{LONG}", b"long", 0o640),
)
TAR = shutil.which("tar")


def _tree(root: Path) -> Path:
    for path, body, mode in FILES:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(body)
        (root / path).chmod(mode)
    (root / "sub/hard").hardlink_to(root / "a")
    (root / "sub/sym").symlink_to("../a")
    return root


def _stored(root: Path) -> StoredTree:
    tar = subprocess.run(  # noqa: S603 - fixed argv
        [str(TAR), "-cf", "-", "-C", str(root), "."],
        capture_output=True,
        check=True,
        # macOS tar would add AppleDouble `._` entries for extended metadata.
        env={**os.environ, "COPYFILE_DISABLE": "1"},
    )
    stored = asyncio.run(store_tar(chunked(tar.stdout, lambda: 4096), MemoryArtifacts()))
    assert isinstance(stored, Ok), stored
    return stored.value


def _file(path: str, body: bytes, mode: int) -> TreeFile:
    sha = hashlib.sha256(body).hexdigest()
    return TreeFile(path=path, kind="file", mode=mode, size=len(body), sha256=sha)


@pytest.mark.skipif(TAR is None, reason="no tar on this host")
def test_the_host_tar_reads_as_the_tree_on_disk(tmp_path: Path) -> None:
    entries = _stored(_tree(tmp_path)).tree.entries
    assert len(entries) == len(FILES) + 4, entries
    dirs = [e for e in entries if isinstance(e, TreeDir)]
    assert [d.path for d in dirs] == ["sub", "sub/déjà vu"]
    assert [e for e in entries if not isinstance(e, TreeDir)] == sorted(
        [
            *(_file(p, b, m) for p, b, m in FILES),
            _file("sub/hard", b"hello", 0o755),
            TreeSymlink(path="sub/sym", kind="symlink", target="../a"),
        ],
        key=lambda e: e.path.encode("utf-16-be"),
    )

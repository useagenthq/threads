"""Fixtures for the sandbox adapter tests."""

import pathlib

import pytest

from threads.adapters.sandboxes.docker import create
from threads.log.digest import sha256_hex

BUILDS = ("linux-amd64", "linux-arm64")


@pytest.fixture
def stub_supervisor(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """The offline Docker suite injects a stub, not the shipped binary.

    The two binaries are build output (gitignored), so a clean checkout has neither, and these
    are unit tests against a mocked daemon that never runs what they inject. The check that
    matters still runs end to end — the adapter hashes what it is about to inject and refuses a
    mismatch — just over bytes the test wrote. The live tests use the real binaries.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    pinned: dict[str, str] = {}
    for build in BUILDS:
        body = f"#!/bin/false\nthreads supervise stub ({build})\n".encode()
        (directory / f"supervise-{build}").write_bytes(body)
        pinned[build] = sha256_hex(body)
    monkeypatch.setattr(create, "BIN_DIR", directory)
    monkeypatch.setattr(create, "BINARIES", pinned)
    return directory

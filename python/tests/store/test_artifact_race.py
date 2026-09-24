"""spec/schema/README.md, "Artifacts": a put refreshes and re-links, gc deletes only trash names,
a reader restores from trash. Each interleaving of plans/specs/lanes/24 Tests 4a is forced through
the store's test seams; the control runs the old behaviour into the same interleavings and loses
the artifact."""

import hashlib
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from threads.result import Ok
from threads.store.artifacts import FileArtifacts
from threads.store.retention import sweep
from threads.store.trash import SEAMS, SeamPoint, trash_name
from threads.store.worker import StoreError

DATA = b'{"name":"mcp__jira__create_issue"}'
SHA = hashlib.sha256(DATA).hexdigest()
DAY = 86_400.0
GRACE = DAY


@dataclass(frozen=True, slots=True)
class Setup:
    root: Path
    store: FileArtifacts
    dir: Path
    path: Path


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    SEAMS.hook = None
    SEAMS.rules = True


def _backdate(path: Path) -> None:
    then = time.time() - 30 * DAY
    os.utime(path, (then, then))


def _setup(tmp_path: Path) -> Setup:
    store = FileArtifacts(tmp_path)
    store.put(DATA)
    path = store.path(SHA)
    _backdate(path)
    return Setup(tmp_path, store, path.parent, path)


def _at(point: SeamPoint, step: Callable[[], object]) -> None:
    """Runs `step` the first time the store reaches `point`."""
    fired: list[bool] = []

    def hook(p: SeamPoint) -> None:
        if p == point and not fired:
            fired.append(True)
            step()

    SEAMS.hook = hook


def _trash(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if ".trash-" in p.name]


def _code(s: Setup) -> str:
    got = s.store.get(SHA)
    return "ok" if isinstance(got, Ok) else got.error.code


def test_a_put_of_an_existing_artifact_refreshes_before_it_verifies(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    seen: list[float] = []
    _at("put:verify", lambda: seen.append(s.path.stat().st_mtime))
    s.store.put(DATA)
    assert seen[0] > time.time() - DAY


def test_easy_order_reput_artifacts_survive_a_gc_before_thread_started(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    s.store.put(DATA)  # thread B re-puts what the deleted thread A named
    assert sweep(s.root, frozenset(), GRACE) == ()
    assert _code(s) == "ok"


def test_i1_gc_sees_a_refreshed_file_young_after_its_rename(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    _at("gc:stated", lambda: s.store.put(DATA))
    assert sweep(s.root, frozenset(), GRACE) == ()
    assert _code(s) == "ok"
    assert _trash(s.dir) == []


def test_i2_a_put_relinks_when_gc_renamed_during_its_refresh(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    _at("put:exists", lambda: sweep(s.root, frozenset(), GRACE))
    assert s.store.put(DATA) == SHA
    assert s.path.exists()
    assert _code(s) == "ok"


def test_i3_a_put_relinks_when_gc_renamed_before_its_verify(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    _at("put:verify", lambda: s.path.rename(s.dir / trash_name(SHA)))
    assert s.store.put(DATA) == SHA
    assert _code(s) == "ok"


def test_i4_a_reader_restores_from_trash_and_gc_unlinks_only_the_trash(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    seen: list[str] = []
    _at("gc:renamed", lambda: seen.append(_code(s)))
    assert sweep(s.root, frozenset(), GRACE) == (SHA,)
    assert seen == ["ok"]
    assert s.path.exists()
    assert _code(s) == "ok"


class _KilledError(Exception):
    pass


def test_i5_a_killed_gc_leaves_trash_that_get_restores_and_a_later_gc_clears(
    tmp_path: Path,
) -> None:
    s = _setup(tmp_path)

    def kill() -> None:
        raise _KilledError

    _at("gc:renamed", kill)
    with pytest.raises(_KilledError):
        sweep(s.root, frozenset(), GRACE)
    assert not s.path.exists()
    assert _code(s) == "ok"
    for name in _trash(s.dir):
        _backdate(s.dir / name)
    _backdate(s.path)
    assert sweep(s.root, frozenset({SHA}), GRACE) == ()
    assert _trash(s.dir) == []
    assert _code(s) == "ok"


def test_control_i1(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    SEAMS.rules = False
    _at("gc:stated", lambda: s.store.put(DATA))
    sweep(s.root, frozenset(), GRACE)
    assert _code(s) == "artifact_missing"


def test_control_i2(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    SEAMS.rules = False
    _at("put:exists", lambda: sweep(s.root, frozenset(), GRACE))
    with pytest.raises(StoreError):
        s.store.put(DATA)
    assert _code(s) == "artifact_missing"


def test_control_i3(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    SEAMS.rules = False
    _at("put:verify", lambda: s.path.rename(s.dir / trash_name(SHA)))
    with pytest.raises(StoreError):
        s.store.put(DATA)
    assert _code(s) == "artifact_missing"


def test_control_i4(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    SEAMS.rules = False
    seen: list[str] = []
    _at("gc:stated", lambda: seen.append(_code(s)))
    sweep(s.root, frozenset(), GRACE)
    assert seen == ["ok"]
    assert _code(s) == "artifact_missing"


def test_reputting_500_existing_artifacts_creates_no_new_file_or_link(tmp_path: Path) -> None:
    store = FileArtifacts(tmp_path)
    blobs = [f"spec {i}".encode() for i in range(500)]
    for blob in blobs:
        store.put(blob)

    def links() -> list[int]:
        return sorted(p.stat().st_nlink for p in (tmp_path / "sha256").glob("*/*"))

    before = links()
    for blob in blobs:
        store.put(blob)
    assert links() == before
    assert before == [1] * 500

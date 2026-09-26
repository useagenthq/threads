"""Portable bundles (spec/schema/README.md, "Portable bundles"): `Thread.export` writes
`log.jsonl` plus every artifact the chain names into a directory, and `import_thread` stores one
anywhere. Export claims the path with an exclusive mkdir, fsyncs what it writes and publishes by
renaming `bundle.json` into place last, so a crash leaves a directory import refuses rather than
a bundle that looks whole.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue, ValidationError

from threads._generated.bundle_v1 import Bundle
from threads.log import BranchId, ParseError
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.result import Err, Ok
from threads.store.artifacts import fsync_dir
from threads.store.sqlite import SqliteStore
from threads.thread.case_log import artifact_refs

MANIFEST = "bundle.json"
"""The manifest's file name. A directory without it is an incomplete bundle, never a bundle."""
LOG_FILE = "log.jsonl"
"""The bundle's log, the same bytes `threads export` writes to stdout."""
ARTIFACTS_DIR = "artifacts"
"""The bundle's artifact directory; each file is named by its lowercase hex sha256."""


@dataclass(frozen=True, slots=True)
class ExportedBundle:
    """spec/api.json ExportedBundle: where the bundle is, what it holds."""

    path: str
    branch_id: BranchId
    artifacts: int


async def export_bundle(
    sq: SqliteStore, branch_id: BranchId, path: str
) -> Ok[ExportedBundle] | Err[ParseError]:
    """The branch's chain and every artifact it names, written to `path`, which must not exist."""
    collected = await _collect(sq, branch_id)
    if isinstance(collected, Err):
        return collected
    log, files = collected.value
    root = Path(path)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        return Err(ParseError("path_exists", f"{path} already exists"))
    except OSError as error:
        return Err(_io(path, error))
    try:
        return Ok(await sq.offload(lambda: _publish(root, branch_id, log, files)))
    except OSError as error:
        _remove(root)
        return Err(_io(path, error))


async def _collect(
    sq: SqliteStore, branch_id: BranchId
) -> Ok[tuple[bytes, dict[str, bytes]]] | Err[ParseError]:
    """The chain's bytes and every artifact it names, read and verified before anything
    is written."""
    exported = await sq.export(branch_id)
    if isinstance(exported, Err):
        return exported
    files: dict[str, bytes] = {}
    for sha256 in artifact_refs(exported.value):
        got = await sq.get_artifact(sha256)
        if isinstance(got, Err):
            return got
        files[sha256] = got.value
    return Ok((exported.value, files))


def _publish(
    root: Path, branch_id: BranchId, log: bytes, files: Mapping[str, bytes]
) -> ExportedBundle:
    """Files, then fsyncs, then the manifest: the bundle exists the instant bundle.json lands."""
    _durable(root / LOG_FILE, log)
    artifacts = root / ARTIFACTS_DIR
    artifacts.mkdir(mode=0o700)
    for sha256, data in files.items():
        _durable(artifacts / sha256, data)
    fsync_dir(artifacts)
    manifest: JsonValue = {
        "format": 1,
        "branch_id": str(branch_id),
        "log_sha256": sha256_hex(log),
        "artifacts": {sha: len(data) for sha, data in files.items()},
    }
    temp = root / f".{MANIFEST}.tmp"
    _durable(temp, _canonical(manifest))
    temp.rename(root / MANIFEST)
    fsync_dir(root)
    fsync_dir(root.parent)
    return ExportedBundle(str(root), branch_id, len(files))


def _canonical(value: JsonValue) -> bytes:
    match canonicalize(value):
        case Ok(value=text):
            return text.encode("utf-8")
        case Err(error=reason):
            raise ValueError(f"a manifest is JSON: {reason}")


def _durable(file: Path, data: bytes) -> None:
    """One file, written and fsynced before the next."""
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove(root: Path) -> None:
    """What a failed export wrote, best effort: a directory with no bundle.json is refused
    anyway."""
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            path.rmdir() if path.is_dir() else path.unlink()
        except OSError:
            return
    try:
        root.rmdir()
    except OSError:
        return


@dataclass(frozen=True, slots=True)
class Read:
    """A bundle's bytes, checked against its manifest."""

    log: bytes
    artifacts: Mapping[str, bytes]


def read_bundle(path: str) -> Ok[Read] | Err[ParseError]:
    """A bundle directory checked against its manifest, or a bare .jsonl export on its own."""
    root = Path(path)
    try:
        if not root.is_dir():
            return Ok(Read(root.read_bytes(), {}))
        return _manifested(root)
    except OSError as error:
        return Err(_io(path, error))


def _manifested(root: Path) -> Ok[Read] | Err[ParseError]:
    file = root / MANIFEST
    if not file.is_file():
        return Err(_incomplete(f"{root} has no {MANIFEST}; the export never finished"))
    try:
        manifest = Bundle.model_validate_json(file.read_bytes())
    except ValidationError as invalid:
        return Err(_incomplete(f"{MANIFEST} is not a manifest: {invalid.error_count()} errors"))
    log = root / LOG_FILE
    if not log.is_file():
        return Err(_incomplete(f"{root} has no {LOG_FILE}"))
    data = log.read_bytes()
    same = _check(LOG_FILE, data, manifest.log_sha256, None)
    if same is not None:
        return Err(same)
    files = _bundled(root, manifest)
    return files if isinstance(files, Err) else Ok(Read(data, files.value))


def _bundled(root: Path, manifest: Bundle) -> Ok[dict[str, bytes]] | Err[ParseError]:
    """Each artifacts/<sha256> the manifest lists, checked against its hash and length."""
    files: dict[str, bytes] = {}
    for sha256, size in manifest.artifacts.items():
        named = root / ARTIFACTS_DIR / sha256
        if not named.is_file():
            return Err(_incomplete(f"{root} is missing {ARTIFACTS_DIR}/{sha256}"))
        body = named.read_bytes()
        bad = _check(f"{ARTIFACTS_DIR}/{sha256}", body, sha256, size)
        if bad is not None:
            return Err(bad)
        files[sha256] = body
    return Ok(files)


def _check(name: str, data: bytes, sha256: str, size: int | None) -> ParseError | None:
    """A bundled file against the manifest: the same bytes, or it is not the one exported."""
    if size is not None and len(data) != size:
        return ParseError("artifact_corrupt", f"{name} is {len(data)} bytes, not {size}")
    if sha256_hex(data) != sha256:
        return ParseError("artifact_corrupt", f"{name} fails its hash")
    return None


def missing_refs(export: bytes, files: Mapping[str, bytes]) -> Sequence[str]:
    """Refs the chain names that the bundle doesn't carry; the store may still hold them."""
    return [sha for sha in artifact_refs(export) if sha not in files]


def _incomplete(why: str) -> ParseError:
    return ParseError("bundle_incomplete", why)


def _io(path: str, error: OSError) -> ParseError:
    return ParseError("io_error", f"{path}: {error}")

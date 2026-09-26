"""`import_thread` (spec/api.json): a bundle directory or a bare .jsonl export, stored in this
store. Everything is validated before anything is written; then the artifacts, then all the rows
in one transaction. A failure after prevalidation can leave artifact files with no rows naming
them: they are content-addressed, never read without a ref, and reused by a retry.
"""

from collections.abc import Mapping

from threads.agents.store import Store, now_ms, open_store
from threads.log import ParseError
from threads.render import ReadArtifact
from threads.render.verify import verify_requests
from threads.result import Err, Ok
from threads.store.import_rows import deleted
from threads.store.sqlite import SqliteStore
from threads.store.verify import VerifiedLog, verify_export
from threads.thread.bundle import Read, missing_refs, read_bundle
from threads.thread.handle import Thread


async def import_thread(store: Store, path: str) -> Ok[Thread] | Err[ParseError]:
    """spec/api.json `importThread`: stores the bundle (or bare export) at `path` and returns a
    handle on its leaf branch. Nothing is written until the whole bundle verifies."""
    sq = await open_store(store)
    read = read_bundle(path)
    if isinstance(read, Err):
        return read
    checked = await _prevalidate(sq, read.value)
    if isinstance(checked, Err):
        return checked
    for data in read.value.artifacts.values():
        await sq.put_artifact(data)
    stored = await sq.import_log(checked.value)
    if isinstance(stored, Err):
        return stored
    leaf = checked.value.segments[-1].header
    return Ok(Thread(leaf.thread_id, leaf.branch_id, store))


async def _prevalidate(sq: SqliteStore, read: Read) -> Ok[VerifiedLog] | Err[ParseError]:
    """The log, its model requests and every artifact it names, checked with nothing written:
    the bundle's files stand in for artifacts the store doesn't hold yet."""
    verified = verify_export(read.log, now_ms())
    if isinstance(verified, Err):
        return verified
    events = verified.value.fold.events
    replayed = await sq.reading(lambda get: verify_requests(events, _overlay(get, read.artifacts)))
    if isinstance(replayed, Err):
        return replayed
    for sha256 in missing_refs(read.log, read.artifacts):
        got = await sq.get_artifact(sha256)
        if isinstance(got, Err):
            return got
    gone = await sq.deleted_thread([s.header.thread_id for s in verified.value.segments])
    if gone is not None:
        return Err(deleted(gone))
    return verified


def _overlay(read: ReadArtifact, files: Mapping[str, bytes]) -> ReadArtifact:
    """The store's artifacts with the bundle's laid over them; every bundled file has already
    been checked against the manifest."""

    def get(sha256: str) -> Ok[bytes] | Err[ParseError]:
        data = files.get(sha256)
        return read(sha256) if data is None else Ok(data)

    return get

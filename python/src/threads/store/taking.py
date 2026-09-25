"""Taking a stored branch's lease for a new writer (SqliteStore.acquire, repair_torn): only this
implementation's major version writes a branch, and a torn import's first append records its
repair."""

from typing import TYPE_CHECKING

from threads import VERSION
from threads.log import ParseError
from threads.result import Err, Ok
from threads.store import lease
from threads.store.artifacts import ArtifactStore
from threads.store.lines import Draft
from threads.store.verify import VerifiedLog
from threads.store.worker import Clock, Worker
from threads.store.writer import Writer

if TYPE_CHECKING:
    from pydantic import JsonValue


async def take(
    worker: Worker, artifacts: ArtifactStore, log: VerifiedLog, holder_id: str, clock: Clock
) -> Ok[Writer] | Err[ParseError]:
    """The writer of `log`'s branch under a new lease, if this implementation writes it."""
    branch_id = log.segments[-1].header.branch_id
    writer = log.segments[-1].header.writer
    if (writer.impl, _major(writer.version)) != ("threads-py", _major(VERSION)):
        message = "another implementation or major version writes this branch; fork it"
        return Err(ParseError("writer_mismatch", message, 0))
    chain_epoch = log.fold.epoch
    taken = await worker.call(lambda c: lease.take(c, branch_id, holder_id, chain_epoch, clock()))
    if isinstance(taken, ParseError):
        return Err(taken)
    return Ok(Writer(worker, taken, log.fold, log.segments[-1].last_line, clock, artifacts.get))


def repair_draft(log: VerifiedLog, dropped_sha256: str) -> Draft:
    """The log_repaired a torn import's first append records."""
    data: dict[str, JsonValue] = {
        "truncated_bytes": len(log.dropped),
        "at_offset": log.committed_bytes,
        "dropped_ref": {
            "sha256": dropped_sha256,
            "bytes": len(log.dropped),
            "media_type": "application/octet-stream",
        },
    }
    return Draft("log_repaired", data, {"kind": "recovery"}, critical=False)


def _major(version: str) -> str:
    return version.split(".", 1)[0]

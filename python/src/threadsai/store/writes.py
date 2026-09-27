"""Two shapes every write in this store shares: pairing an artifact put with the row that
names it, and turning a parse error into the Ok/Err a caller expects."""

from collections.abc import Callable

from threadsai.log import ParseError
from threadsai.redaction import published
from threadsai.result import Err, Ok
from threadsai.store.artifacts import ArtifactStore
from threadsai.store.conn import Conn


def stored_first[T](
    artifacts: ArtifactStore, data: bytes | None, job: Callable[[Conn], T]
) -> Callable[[Conn], T]:
    """`job`, after `data` (if any) is stored as an artifact with registration paused."""
    if data is None:
        return job
    stored = data

    def both(conn: Conn) -> T:
        artifacts.put(stored)
        return job(conn)

    return lambda c: published(stored, lambda: both(c))


def result(error: ParseError | None) -> Ok[None] | Err[ParseError]:
    return Ok(None) if error is None else Err(error)

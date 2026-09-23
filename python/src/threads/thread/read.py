"""A branch's log as a reader of the public handle sees it."""

from threads.agents.store import Store, now_ms, open_store
from threads.log import BranchId, ParseError
from threads.result import Err, Ok
from threads.store import VerifiedLog


async def read_log(store: Store, branch: BranchId) -> Ok[VerifiedLog] | Err[ParseError]:
    """The branch's verified log, or `read_error` of why it can't be read."""
    read = await (await open_store(store)).read(branch, now_ms())
    return read if isinstance(read, Ok) else Err(read_error(read.error))


def read_error(error: ParseError) -> ParseError:
    """A line from a newer writer stays unsupported; any other failure, a branch gone missing
    included, is log_corrupt (TS readLog)."""
    match error.code:
        case "unsupported_format" | "unsupported_critical_event":
            return error
        case _:
            return ParseError("log_corrupt", f"{error.code}: {error.message}", error.seq)

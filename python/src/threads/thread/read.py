"""A branch's log as a reader of the public handle sees it."""

from threads.agents.store import Store, now_ms, open_store
from threads.log import BranchId, ParseError
from threads.result import Err, Ok
from threads.store import VerifiedLog


async def read_log(store: Store, branch: BranchId) -> Ok[VerifiedLog] | Err[ParseError]:
    """The branch's verified log. A branch that fails verification is log_corrupt."""
    read = await (await open_store(store)).read(branch, now_ms())
    return read if isinstance(read, Ok) else Err(reader_error(read.error))


def reader_error(error: ParseError) -> ParseError:
    """Any failure but a missing branch or an unsupported line is log_corrupt."""
    match error.code:
        case "branch_not_found":
            return ParseError("not_found", error.message)
        case "unsupported_format" | "unsupported_critical_event":
            return error
        case _:
            return ParseError("log_corrupt", f"{error.code}: {error.message}", error.seq)

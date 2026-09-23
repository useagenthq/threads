"""Where a sandbox adapter fences (spec/api.json `SandboxContext`; ): at the
SDK's real transport, never before the SDK call.

An operation binds its context with `dispatch` around the SDK calls it makes. Each SDK's
transport hook (an httpx trace, an aiohttp trace, a pyqwest transport, a grpclib SendRequest
listener) awaits `check()` at its send point, after the client's own queueing. A failed fence
raises there, so no byte of the request is written, and the operation answers the typed refusal
(stale_epoch or cleanup_claim_lost). A request outside any operation is an adapter bug.
"""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from threads.result import Err, Ok
from threads.sandbox.protocol import SandboxContext, SandboxError, refused

_bound: ContextVar[SandboxContext | None] = ContextVar("threads_sandbox_operation", default=None)

type Classify = Callable[[Exception], SandboxError | None]
"""Names an SDK exception's expected failure (unavailable, not_found, ...); None re-raises it."""


class FenceRefusedError(Exception):
    """The fence failed at a send point: nothing of that request was written."""

    def __init__(self, error: SandboxError) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.error = error


class UnfencedRequestError(Exception):
    """A provider request outside any operation: an adapter bug, never a runtime state."""


async def check() -> None:
    """The transport's send point: raises unless the bound context's fence still passes."""
    context = _bound.get()
    if context is None:
        raise UnfencedRequestError("a sandbox provider request outside an operation")
    refusal = await refused(context)
    if refusal is not None:
        raise FenceRefusedError(refusal.error)


async def dispatch[T](
    context: SandboxContext, call: Callable[[], Awaitable[T]], classify: Classify
) -> Ok[T] | Err[SandboxError]:
    """Runs one provider operation's SDK calls with `context` bound for their send points."""
    token = _bound.set(context)
    try:
        return Ok(await call())
    except Exception as error:
        refusal = _refusal(error)
        if refusal is not None:
            return Err(refusal)
        known = classify(error)
        if known is None:
            raise
        return Err(known)
    finally:
        _bound.reset(token)


def _refusal(error: BaseException) -> SandboxError | None:
    """SDKs wrap a transport exception in their own: find ours in the chain."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, FenceRefusedError):
            return cause.error
        cause = cause.__cause__ or cause.__context__
    return None

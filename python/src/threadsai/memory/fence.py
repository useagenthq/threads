"""The fence a provider adapter checks at its real transport.

The framework binds the run's fence around every provider call it makes (`bound`). An adapter's
HTTP transport awaits `check()` at its send point, after the SDK's own queueing, and raises
`FenceRefusedError` there, so no byte of that request is written. A request outside any
provider call is refused too: an adapter never sends unfenced.
"""

from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar

type Fence = Callable[[], Awaitable[bool]]

_bound: ContextVar[Fence | None] = ContextVar("threads_provider_fence", default=None)


class FenceRefusedError(Exception):
    """The fence failed at a send point: nothing of that request was written."""


@contextmanager
def bound(fence: Fence) -> Generator[None]:
    token = _bound.set(fence)
    try:
        yield
    finally:
        _bound.reset(token)


async def check() -> None:
    """The transport's send point: raises unless the bound fence still passes."""
    fence = _bound.get()
    if fence is None or not await fence():
        raise FenceRefusedError("stale_epoch: this run no longer owns the branch")


def refused(error: BaseException) -> bool:
    """SDKs wrap a transport exception in their own: find ours in the chain."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, FenceRefusedError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False

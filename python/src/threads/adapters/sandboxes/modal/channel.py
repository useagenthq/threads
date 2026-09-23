"""gRPC channels to Modal, fenced at the send point, and what their failures mean.

grpclib dispatches `SendRequest` after the connection is up and the HTTP/2 stream exists, right
before the request headers are written (grpclib/client.py `Stream.send_request`). The fence runs
there, so a refused operation writes no byte of its request.
"""

import asyncio
import ssl
import urllib.parse
from collections.abc import Callable

import grpclib.client
import grpclib.events
from grpclib.const import Status
from grpclib.exceptions import GRPCError, ProtocolError, StreamTerminatedError

from threads.adapters.sandboxes import fence
from threads.sandbox.protocol import SandboxError

type Connect = Callable[[str], grpclib.client.Channel]
"""Opens an (unfenced) channel to an https URL; a test hook replaces it."""


class MalformedResponseError(Exception):
    """A Modal response missing what the adapter relies on."""


class SandboxEndedError(Exception):
    """The sandbox has a result: it ended, so it has no task to run anything on."""


def connect(url: str) -> grpclib.client.Channel:
    """A TLS channel to `url`, which must be https (a provider-supplied router URL included)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MalformedResponseError(f"not an https URL: {url}")
    context = ssl.create_default_context()
    return grpclib.client.Channel(parsed.hostname, parsed.port or 443, ssl=context)


def fenced(channel: grpclib.client.Channel) -> grpclib.client.Channel:
    """Every request on `channel` awaits the bound operation's fence before its headers go."""

    async def on_send(_event: grpclib.events.SendRequest) -> None:
        await fence.check()

    grpclib.events.listen(channel, grpclib.events.SendRequest, on_send)
    return channel


def classify(error: Exception) -> SandboxError | None:
    """A Modal call's failure as a typed error; None for anything that is a bug."""
    match error:
        case GRPCError(status=Status.NOT_FOUND):
            return SandboxError("not_found", error.message or "not found")
        case SandboxEndedError():
            return SandboxError("not_found", str(error))
        case GRPCError(status=Status.DEADLINE_EXCEEDED) | asyncio.TimeoutError():
            return SandboxError("timeout", str(error))
        case GRPCError() | StreamTerminatedError() | ProtocolError() | OSError():
            return SandboxError("unavailable", f"modal: {error}")
        case MalformedResponseError():
            return SandboxError("unavailable", f"modal: {error}")
        case _:
            return None


def status_of(error: Exception) -> Status | None:
    return error.status if isinstance(error, GRPCError) else None

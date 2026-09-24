"""The E2B control plane through the SDK's generated client (e2b.api.client), on a fenced
transport. Every call is one attempt: nothing here retries, a lost answer is the ledger's to
settle by the operation key. Responses are parsed with wire.py; statuses the operation expects
are values, any other is an ApiError."""

import urllib.parse
from collections.abc import Awaitable
from dataclasses import dataclass
from http import HTTPStatus

from e2b.api import AsyncApiClient
from e2b.api.client.api.sandboxes import (
    delete_sandboxes_sandbox_id,
    get_sandboxes_sandbox_id,
    get_v2_sandboxes,
    post_v2_sandboxes,
)
from e2b.api.client.models import NewSandboxV2, SandboxState
from e2b.api.client.types import Response

from threads.adapters.sandboxes.e2b import wire

KEY = "threads_operation_key"
"""The sandbox metadata entry that names the operation that created it."""


class MalformedError(Exception):
    """The SDK's generated parser failed on a response body: nothing is established by it."""


class ApiError(Exception):
    """A status the operation doesn't expect: nothing is established by it."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"E2B API {status}: {body[:500].decode('utf-8', 'replace')}")
        self.status = status


@dataclass(frozen=True, slots=True)
class Control:
    client: AsyncApiClient

    async def create(
        self, template: str, key: str, *, timeout_s: int, internet: bool
    ) -> wire.Sandbox | None:
        """A new sandbox from `template` (a template or snapshot id), tagged with `key` and no
        environment. None: E2B has no such template."""
        body = NewSandboxV2(
            template_id=template,
            timeout=timeout_s,
            metadata={KEY: key},
            env_vars={},
            allow_internet_access=internet,
        )
        res = await _sent(post_v2_sandboxes.asyncio_detailed(client=self.client, body=body))
        if res.status_code == HTTPStatus.NOT_FOUND:
            return None
        return wire.Sandbox.model_validate_json(_expect(res, HTTPStatus.CREATED))

    async def find(self, key: str) -> list[str]:
        """The live or paused sandboxes created under `key`."""
        metadata = urllib.parse.urlencode({urllib.parse.quote(KEY): urllib.parse.quote(key)})
        res = await _sent(
            get_v2_sandboxes.asyncio_detailed(
                client=self.client,
                metadata=metadata,
                state=[SandboxState.RUNNING, SandboxState.PAUSED],
            )
        )
        listed = wire.LISTED.validate_json(_expect(res, HTTPStatus.OK))
        return [s.sandbox_id for s in listed if s.metadata and s.metadata.get(KEY) == key]

    async def describe(self, sandbox_id: str) -> wire.Sandbox | None:
        """None: no such sandbox."""
        res = await _sent(get_sandboxes_sandbox_id.asyncio_detailed(sandbox_id, client=self.client))
        if res.status_code == HTTPStatus.NOT_FOUND:
            return None
        return wire.Sandbox.model_validate_json(_expect(res, HTTPStatus.OK))

    async def kill(self, sandbox_id: str) -> bool:
        """False: it was already gone."""
        res = await _sent(
            delete_sandboxes_sandbox_id.asyncio_detailed(sandbox_id, client=self.client)
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            return False
        _expect(res, HTTPStatus.NO_CONTENT)
        return True


async def _sent[T](call: Awaitable[Response[T]]) -> Response[T]:
    """The SDK parses every documented status's body before returning; a body it can't
    parse is a malformed answer, not an adapter bug."""
    try:
        return await call
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedError(f"E2B sent a body its SDK can't parse: {error!r}") from error


def _expect[T](res: Response[T], status: HTTPStatus) -> bytes:
    if res.status_code != status:
        raise ApiError(res.status_code, res.content)
    return res.content

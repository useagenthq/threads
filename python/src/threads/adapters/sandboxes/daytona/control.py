"""The Daytona control plane through the official generated async client: sandboxes and
snapshots, each call one request on the fenced session. Callers run these inside
`fence.dispatch`; a non-2xx answer raises `StatusError` for the operation to map."""

import asyncio
from collections.abc import Awaitable, Collection
from dataclasses import dataclass

import aiohttp
from daytona_api_client_async import ApiClient, Configuration, SandboxApi, SnapshotsApi
from daytona_api_client_async.models.create_sandbox import CreateSandbox
from daytona_api_client_async.models.create_sandbox_snapshot import CreateSandboxSnapshot
from pydantic import ValidationError

from threads.adapters.sandboxes.daytona.wire import SandboxDto, SnapshotDto
from threads.sandbox.protocol import SandboxError

NOT_FOUND = 404
CONFLICT = 409
_FAILED = frozenset({"error", "build_failed"})
_GONE = frozenset({"destroyed", "destroying"})


class StatusError(Exception):
    """Daytona answered with an error status."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"daytona answered {status}: {body[:200]!r}")
        self.status = status


def classify(error: Exception) -> SandboxError | None:
    """What an SDK or transport failure means: nothing here proves the provider didn't act, so
    each is `unavailable` (a create's key lookup resolves it)."""
    if isinstance(error, StatusError | aiohttp.ClientError | TimeoutError | ValidationError):
        return SandboxError("unavailable", str(error))
    return None


async def body(sent: Awaitable[aiohttp.ClientResponse]) -> bytes:
    response = await sent
    async with response:
        data = await response.read()
    if response.status >= 300:  # noqa: PLR2004 - HTTP success range
        raise StatusError(response.status, data)
    return data


def client(api_url: str, session: aiohttp.ClientSession) -> ApiClient:
    """The generated client on our session (which carries the credentials): SDK retries off
    (Configuration.retries None)."""
    api = ApiClient(Configuration(host=api_url))
    api.rest_client.pool_manager = session
    return api


@dataclass(frozen=True, slots=True)
class Placement:
    """How every sandbox is created: region, provider expiry and network."""

    target: str | None
    ttl_minutes: int | None
    block_network: bool


class Control:
    def __init__(self, api: ApiClient, poll_s: float, wait_s: float) -> None:
        self._sandboxes = SandboxApi(api)
        self._snapshots = SnapshotsApi(api)
        self._poll_s, self._wait_s = poll_s, wait_s

    async def create(self, name: str, snapshot: str | None, placed: Placement) -> SandboxDto:
        """Creates `name`, or finds it: a name is unique per organization, so a create that
        already happened answers 409 and is the same sandbox."""
        request = CreateSandbox(
            name=name,
            snapshot=snapshot,
            target=placed.target,
            ttl_minutes=placed.ttl_minutes,
            network_block_all=placed.block_network,
            env={},
        )
        try:
            await body(self._sandboxes.create_sandbox_without_preload_content(request))
        except StatusError as error:
            if error.status != CONFLICT:
                raise
        return await self.until(name, {"started"})

    async def get(self, ref: str) -> SandboxDto | None:
        """None: the provider says it doesn't exist (404, or destroyed)."""
        try:
            data = await body(self._sandboxes.get_sandbox_without_preload_content(ref))
        except StatusError as error:
            if error.status == NOT_FOUND:
                return None
            raise
        found = SandboxDto.model_validate_json(data)
        return None if found.state in _GONE else found

    async def until(self, ref: str, states: Collection[str]) -> SandboxDto:
        """Polls until the sandbox reaches one of `states`."""
        async with asyncio.timeout(self._wait_s):
            while True:
                found = await self.get(ref)
                if found is None or found.state in _FAILED:
                    raise StatusError(NOT_FOUND, f"{ref} is {found and found.state}".encode())
                if found.state in states:
                    return found
                await asyncio.sleep(self._poll_s)

    async def delete(self, ref: str) -> bool:
        """Deletes and waits for it to be destroyed; False: it was already gone."""
        try:
            await body(self._sandboxes.delete_sandbox_without_preload_content(ref))
        except StatusError as error:
            if error.status == NOT_FOUND:
                return False
            raise
        async with asyncio.timeout(self._wait_s):
            while await self.get(ref) is not None:
                await asyncio.sleep(self._poll_s)
        return True

    async def stop(self, ref: str) -> None:
        await body(self._sandboxes.stop_sandbox_without_preload_content(ref))
        await self.until(ref, {"stopped"})

    async def start(self, ref: str) -> SandboxDto:
        await body(self._sandboxes.start_sandbox_without_preload_content(ref))
        return await self.until(ref, {"started"})

    async def capture(self, ref: str, name: str) -> SnapshotDto:
        """A cold snapshot of a stopped sandbox, once active."""
        request = CreateSandboxSnapshot(name=name)
        await body(self._sandboxes.create_sandbox_snapshot_without_preload_content(ref, request))
        await self.until(ref, {"stopped"})
        async with asyncio.timeout(self._wait_s):
            while (snap := await self.snapshot(name)) is None or snap.state != "active":
                if snap is not None and snap.state in _FAILED:
                    raise StatusError(NOT_FOUND, f"snapshot {name} is {snap.state}".encode())
                await asyncio.sleep(self._poll_s)
        return snap

    async def snapshot(self, ref: str) -> SnapshotDto | None:
        try:
            data = await body(self._snapshots.get_snapshot_without_preload_content(ref))
        except StatusError as error:
            if error.status == NOT_FOUND:
                return None
            raise
        return SnapshotDto.model_validate_json(data)

    async def remove(self, ref: str) -> bool:
        """Removes a snapshot and waits for it to be gone; False: it was already gone."""
        snap = await self.snapshot(ref)
        if snap is None:
            return False
        await body(self._snapshots.remove_snapshot_without_preload_content(snap.id))
        async with asyncio.timeout(self._wait_s):
            while await self.snapshot(ref) is not None:
                await asyncio.sleep(self._poll_s)
        return True

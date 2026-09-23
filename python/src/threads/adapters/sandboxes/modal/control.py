"""Modal's control plane (modal.client.ModalClient), over the SDK's generated stubs and a fenced
channel. The token travels only as call metadata to the control plane; nothing here passes env,
secrets or credentials to a sandbox."""

import asyncio
import platform
from dataclasses import dataclass
from importlib.metadata import version

import grpclib.client
from modal_proto import api_grpc, api_pb2

from threads.adapters.sandboxes.modal.channel import MalformedResponseError, fenced

_TASK_POLLS = 120
"""Waits of 0.5s for a new sandbox to be scheduled onto a task before answering unavailable."""


@dataclass(frozen=True, slots=True)
class Settings:
    token_id: str
    token_secret: str
    app_name: str
    image_id: str
    lifetime_s: int
    environment: str
    internet: bool = False
    """Off: the sandbox is created with block_network (deny-all egress)."""


@dataclass(frozen=True, slots=True)
class RouterAccess:
    task_id: str
    url: str
    jwt: str


class Control:
    def __init__(self, channel: grpclib.client.Channel, settings: Settings) -> None:
        self._stub = api_grpc.ModalClientStub(fenced(channel))
        self._settings = settings
        self._meta = {
            "x-modal-client-version": version("modal"),
            "x-modal-client-type": str(api_pb2.CLIENT_TYPE_CLIENT),
            "x-modal-python-version": platform.python_version(),
            "x-modal-token-id": settings.token_id,
            "x-modal-token-secret": settings.token_secret,
        }
        self._app_id: str | None = None

    async def create(self, name: str) -> str:
        """A named sandbox: Modal refuses a second running sandbox of one name in an app
        (ALREADY_EXISTS), so a create is idempotent on its name."""
        s = self._settings
        definition = api_pb2.Sandbox(
            image_id=s.image_id,
            timeout_secs=s.lifetime_s,
            name=name,
            block_network=not s.internet,
        )
        request = api_pb2.SandboxCreateRequest(app_id=await self._app(), definition=definition)
        made = await self._stub.SandboxCreate(request, metadata=self._meta)
        return _id(made.sandbox_id, "sandbox_id")

    async def by_name(self, name: str) -> str:
        """The running sandbox of that name; GRPCError NOT_FOUND when none runs."""
        request = api_pb2.SandboxGetFromNameRequest(
            sandbox_name=name,
            app_name=self._settings.app_name,
            environment_name=self._settings.environment,
        )
        found = await self._stub.SandboxGetFromName(request, metadata=self._meta)
        return _id(found.sandbox_id, "sandbox_id")

    async def running(self, sandbox_id: str) -> bool:
        """False once the sandbox has a result (it ended)."""
        request = api_pb2.SandboxWaitRequest(sandbox_id=sandbox_id, timeout=0)
        waited = await self._stub.SandboxWait(request, metadata=self._meta)
        return not (waited.HasField("result") and waited.result.status)

    async def terminate(self, sandbox_id: str) -> None:
        request = api_pb2.SandboxTerminateRequest(sandbox_id=sandbox_id)
        await self._stub.SandboxTerminate(request, metadata=self._meta)

    async def router(self, sandbox_id: str) -> RouterAccess | None:
        """Where the sandbox's task command router listens; None when the sandbox ended."""
        request = api_pb2.SandboxGetTaskIdRequest(sandbox_id=sandbox_id)
        for _ in range(_TASK_POLLS):
            got = await self._stub.SandboxGetTaskId(request, metadata=self._meta)
            if got.task_id:
                break
            if got.HasField("task_result"):
                return None
            await asyncio.sleep(0.5)
        else:
            raise MalformedResponseError(f"{sandbox_id} was never scheduled")
        ask = api_pb2.TaskGetCommandRouterAccessRequest(task_id=got.task_id)
        access = await self._stub.TaskGetCommandRouterAccess(ask, metadata=self._meta)
        return RouterAccess(got.task_id, _id(access.url, "url"), _id(access.jwt, "jwt"))

    async def _app(self) -> str:
        if self._app_id is None:
            request = api_pb2.AppGetOrCreateRequest(
                app_name=self._settings.app_name,
                environment_name=self._settings.environment,
                object_creation_type=api_pb2.OBJECT_CREATION_TYPE_CREATE_IF_MISSING,
            )
            got = await self._stub.AppGetOrCreate(request, metadata=self._meta)
            self._app_id = _id(got.app_id, "app_id")
        return self._app_id


def _id(value: str, field: str) -> str:
    if not value:
        raise MalformedResponseError(f"the response has no {field}")
    return value

"""A sandbox's task command router (modal.task_command_router), where Modal runs execs: start,
stdin, stdout and stderr as separate streams, and the exit. Each exec is a provider process
record (exec_id), but the router has no kill for it and no view of its descendants."""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence

import grpclib.client
from modal_proto import task_command_router_grpc as grpc
from modal_proto import task_command_router_pb2 as pb

from threads.adapters.sandboxes.modal.channel import fenced
from threads.adapters.sandboxes.streams import Pipe

_STDIN_CHUNK = 1 << 20
_SIGNALLED = 128
_OUT = pb.TASK_EXEC_STDIO_FILE_DESCRIPTOR_STDOUT
_ERR = pb.TASK_EXEC_STDIO_FILE_DESCRIPTOR_STDERR


class Router:
    def __init__(self, channel: grpclib.client.Channel, task_id: str, jwt: str) -> None:
        self._stub = grpc.TaskCommandRouterStub(fenced(channel))
        self._task = task_id
        self._meta = {"authorization": f"Bearer {jwt}"}

    async def start(  # noqa: PLR0913 - one exec's settings
        self,
        exec_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        *,
        stdout: bool = True,
        stderr: bool = True,
    ) -> None:
        request = pb.TaskExecStartRequest(
            task_id=self._task,
            exec_id=exec_id,
            command_args=argv,
            stdout_config=pb.TASK_EXEC_STDOUT_CONFIG_PIPE
            if stdout
            else pb.TASK_EXEC_STDOUT_CONFIG_DEVNULL,
            stderr_config=pb.TASK_EXEC_STDERR_CONFIG_PIPE
            if stderr
            else pb.TASK_EXEC_STDERR_CONFIG_DEVNULL,
            workdir=cwd,
            env=env,
        )
        await self._stub.TaskExecStart(request, metadata=self._meta)

    async def write(self, exec_id: str, data: bytes) -> None:
        """All of `data` to the exec's stdin, then EOF."""
        offset = 0
        while True:
            chunk = data[offset : offset + _STDIN_CHUNK]
            last = offset + len(chunk) >= len(data)
            request = pb.TaskExecStdinWriteRequest(
                task_id=self._task, exec_id=exec_id, offset=offset, data=chunk, eof=last
            )
            await self._stub.TaskExecStdinWrite(request, metadata=self._meta)
            offset += len(chunk)
            if last:
                return

    async def read(self, exec_id: str, *, stderr: bool = False) -> AsyncIterator[bytes]:
        request = pb.TaskExecStdioReadRequest(
            task_id=self._task, exec_id=exec_id, offset=0, file_descriptor=_ERR if stderr else _OUT
        )
        async with self._stub.TaskExecStdioRead.open(metadata=self._meta) as stream:
            await stream.send_message(request, end=True)
            async for reply in stream:
                yield reply.data

    async def wait(self, exec_id: str) -> int:
        request = pb.TaskExecWaitRequest(task_id=self._task, exec_id=exec_id)
        done = await self._stub.TaskExecWait(request, metadata=self._meta)
        if done.HasField("signal"):
            return _SIGNALLED + done.signal
        return done.code

    async def feed(self, exec_id: str, pipe: Pipe) -> int:
        """Pumps both streams into `pipe`, then the exit code."""

        async def out() -> None:
            async for chunk in self.read(exec_id):
                await pipe.stdout(chunk)

        async def err() -> None:
            async for chunk in self.read(exec_id, stderr=True):
                await pipe.stderr(chunk)

        await asyncio.gather(out(), err())
        return await self.wait(exec_id)

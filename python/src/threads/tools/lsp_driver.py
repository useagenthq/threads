"""The lsp tool's in-sandbox driver. The host uploads this file and runs it with
the sandbox image's python3, which may be as old as 3.8, so it keeps to that language level and
imports nothing from threads. It starts the language server, opens one file, asks one question
and prints one JSON line: {"result": ...} or {"unavailable": reason}.

    python3 lsp_driver.py '<server argv json>' <root> <path> <language> <operation> [line char]
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from typing import TypeAlias

Json: TypeAlias = "dict[str, Json] | list[Json] | str | int | float | bool | None"  # noqa: UP040 - runs on Python 3.8
Message: TypeAlias = "dict[str, Json]"  # noqa: UP040 - runs on Python 3.8
# Before 3.11, asyncio's timeout is its own class.
Timeout: Final = asyncio.TimeoutError

READY_S: Final = 30.0
SEARCH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
"""Where a bare server command is looked up. The driver runs with the tool env, which has no
PATH, so the default search (/bin:/usr/bin) would miss servers in /usr/local/bin, where pip and
npm put them. Fixed system dirs, never a host PATH."""
QUIET_S: Final = 1.0
REQUESTS: Final = {
    "definition": "textDocument/definition",
    "references": "textDocument/references",
    "hover": "textDocument/hover",
    "symbols": "textDocument/documentSymbol",
}


class Server:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdin is None or proc.stdout is None:
            raise AssertionError("pipes were requested")
        self.proc: asyncio.subprocess.Process = proc
        self.stdin: asyncio.StreamWriter = proc.stdin
        self.inbox: asyncio.Queue[Message] = asyncio.Queue()
        self.reading: asyncio.Future[None] = asyncio.ensure_future(self._read(proc.stdout))

    async def _read(self, stdout: asyncio.StreamReader) -> None:
        while True:
            length = 0
            line = await stdout.readline()
            while line not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode("ascii", "replace").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value)
                line = await stdout.readline()
            if not line:
                return
            message: Json = json.loads(await stdout.readexactly(length))
            if isinstance(message, dict):
                await self.inbox.put(message)

    async def send(self, message: Message) -> None:
        body = json.dumps({"jsonrpc": "2.0", **message}).encode()
        self.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        await self.stdin.drain()

    async def _next(self, wait: float) -> Message | None:
        try:
            return await asyncio.wait_for(self.inbox.get(), wait)
        except Timeout:
            return None

    async def answer(self, n: int) -> Message | None:
        """The response to request `n`, or None if the server stays silent."""
        loop = asyncio.get_running_loop()
        until = loop.time() + READY_S
        while (message := await self._next(until - loop.time())) is not None:
            if message.get("id") == n and "method" not in message:
                return message
        return None

    async def diagnostics(self, uri: str) -> Json:
        """The last publishDiagnostics for the file, once the server is quiet a moment."""
        latest: Json = None
        wait = READY_S
        while (message := await self._next(wait)) is not None:
            params = message.get("params")
            method = message.get("method")
            published = method == "textDocument/publishDiagnostics" and isinstance(params, dict)
            if published and isinstance(params, dict) and params.get("uri") == uri:
                latest, wait = params.get("diagnostics"), QUIET_S
        return latest

    async def close(self) -> None:
        try:
            await self.send({"id": 0, "method": "shutdown"})
            await self.send({"method": "exit"})
            await asyncio.wait_for(self.proc.wait(), QUIET_S)
        except (OSError, Timeout):
            self.proc.kill()
        self.reading.cancel()


async def ask(server: Server, argv: list[str]) -> Json:
    _, root, path, language, operation = argv[:5]
    caps: Json = {"textDocument": {"publishDiagnostics": {}, "hover": {}, "definition": {}}}
    init: Message = {"processId": None, "rootUri": Path(root).as_uri(), "capabilities": caps}
    await server.send({"id": 1, "method": "initialize", "params": init})
    if await server.answer(1) is None:
        return {"unavailable": "the language server did not answer initialize"}
    await server.send({"method": "initialized", "params": {}})
    uri = Path(path).as_uri()
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    doc: Json = {"uri": uri, "languageId": language, "version": 1, "text": text}
    await server.send({"method": "textDocument/didOpen", "params": {"textDocument": doc}})
    if operation == "diagnostics":
        return {"result": await server.diagnostics(uri)}
    params: Message = {"textDocument": {"uri": uri}}
    if len(argv) > 5:  # noqa: PLR2004 - line and character follow the operation
        params["position"] = {"line": int(argv[5]) - 1, "character": int(argv[6]) - 1}
    if operation == "references":
        params["context"] = {"includeDeclaration": True}
    await server.send({"id": 2, "method": REQUESTS[operation], "params": params})
    got = await server.answer(2)
    if got is None:
        return {"unavailable": "the language server did not answer " + operation}
    return {"result": got.get("result", got.get("error"))}


async def main(argv: list[str]) -> Json:
    parsed: Json = json.loads(argv[0])
    command = [a for a in parsed if isinstance(a, str)] if isinstance(parsed, list) else []
    if not command:
        return {"unavailable": "no language server command"}
    command[0] = shutil.which(command[0], path=os.environ.get("PATH", SEARCH)) or command[0]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            cwd=argv[1],
        )
    except OSError as error:
        return {"unavailable": "the language server did not start: " + str(error)}
    server = Server(proc)
    try:
        return await ask(server, argv)
    finally:
        await server.close()


if __name__ == "__main__":
    sys.stdout.write(json.dumps(asyncio.run(main(sys.argv[1:]))) + "\n")

"""The Docker Engine API as typed calls over one event loop's fenced httpx client.

No SDK: the API is HTTP over a unix socket, the version is pinned in every path, and every
answer is parsed (wire.py). Each call is one attempt; a lost answer is resolved by the ledger,
never by a retry that could repeat an effect.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import httpx
from pydantic import JsonValue, ValidationError

from threads.adapters.loop_resources import close_all, drain
from threads.adapters.sandboxes.docker import wire
from threads.adapters.sandboxes.docker.transport import BASE_URL, MakeTransport
from threads.adapters.sandboxes.httpx_fence import FencedHttpx

OK: Final = 200
NOT_FOUND: Final = 404
CONFLICT: Final = 409
BAD_REQUEST: Final = 400
ARCHIVE_MAX: Final = 64 * 1024 * 1024
"""The largest archive read back from a container; beyond it the answer is refused."""
_PUMP_GRACE_S: Final = 0.25


class EngineError(Exception):
    """The daemon refused a call, or answered something this adapter can't use."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"docker: {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class Engine:
    """One event loop's client to the daemon, and the exec streams reading it."""

    client: httpx.AsyncClient
    inner: httpx.AsyncBaseTransport
    """This loop's own transport, closed after the client that sends through it."""
    pumps: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    async def create_container(self, name: str, body: Mapping[str, JsonValue]) -> wire.Created:
        params, allow = {"name": name}, (CONFLICT,)
        sent = await self._send("POST", "/containers/create", params=params, json=body, allow=allow)
        if sent.status_code == CONFLICT:  # the name is this key's earlier create
            raise NameTakenError(name)
        if sent.status_code == NOT_FOUND:  # the image isn't here yet
            raise _refused(sent, sent.content)
        return wire.Created.model_validate_json(self._body(sent))

    async def pull_image(self, ref: str) -> None:
        """Anonymously: no registry credentials are ever sent. The streamed body is drained and
        its last JSON line checked, which is where the daemon reports a failed pull."""
        sent = await self._send("POST", "/images/create", params={"fromImage": ref})
        last = self._body(sent).splitlines()
        line = wire.PullLine.model_validate_json(last[-1]) if last else wire.PullLine()
        if line.error is not None:
            raise EngineError(sent.status_code, f"pulling {ref}: {line.error}")

    async def inspect_container(self, name: str) -> wire.Inspected | None:
        sent = await self._send("GET", f"/containers/{name}/json")
        if sent.status_code == NOT_FOUND:
            return None
        return wire.Inspected.model_validate_json(self._body(sent))

    async def inspect_image(self, ref: str) -> wire.Image | None:
        sent = await self._send("GET", f"/images/{ref}/json")
        if sent.status_code == NOT_FOUND:
            return None
        return wire.Image.model_validate_json(self._body(sent))

    async def start_container(self, name: str) -> None:
        sent = await self._send("POST", f"/containers/{name}/start")
        if sent.status_code == NOT_FOUND:
            raise EngineError(NOT_FOUND, f"no container {name}")

    async def remove_container(self, name: str) -> bool:
        """With its anonymous volumes. False: it was already gone."""
        params = {"force": "1", "v": "1"}
        sent = await self._send("DELETE", f"/containers/{name}", params=params)
        return sent.status_code != NOT_FOUND

    async def remove_volume(self, name: str) -> bool:
        sent = await self._send("DELETE", f"/volumes/{name}", params={"force": "1"})
        return sent.status_code != NOT_FOUND

    async def find_containers(self, label: str) -> tuple[wire.Listed, ...]:
        filters = json.dumps({"label": [label]}, separators=(",", ":"))
        sent = await self._send("GET", "/containers/json", params={"all": "1", "filters": filters})
        return wire.LISTED.validate_json(self._body(sent))

    async def put_archive(self, name: str, path: str, tar: bytes) -> None:
        sent = await self._send(
            "PUT", f"/containers/{name}/archive", params={"path": path}, content=tar
        )
        if sent.status_code == NOT_FOUND:
            raise EngineError(NOT_FOUND, f"{name} has no {path}")

    async def get_archive(self, name: str, path: str) -> bytes | None:
        """The tar of `path`, or None when it doesn't exist."""
        sent = await self._send("GET", f"/containers/{name}/archive", params={"path": path})
        if sent.status_code == NOT_FOUND:
            return None
        return self._body(sent)

    async def exec_create(self, name: str, body: Mapping[str, JsonValue]) -> wire.ExecMade:
        sent = await self._send("POST", f"/containers/{name}/exec", json=body)
        if sent.status_code == NOT_FOUND:
            raise EngineError(NOT_FOUND, f"no container {name}")
        return wire.ExecMade.model_validate_json(self._body(sent))

    async def exec_start(self, exec_id: str) -> httpx.Response:
        """The multiplexed stream, still open: the caller reads and closes it."""
        body = {"Detach": False, "Tty": False}
        request = self.client.build_request("POST", f"/exec/{exec_id}/start", json=body)
        sent = await self.client.send(request, stream=True)
        if sent.status_code != OK:
            await sent.aread()
            await sent.aclose()
            raise _refused(sent, sent.content)
        return sent

    async def exec_state(self, exec_id: str) -> wire.ExecState:
        sent = await self._send("GET", f"/exec/{exec_id}/json")
        return wire.ExecState.model_validate_json(self._body(sent))

    async def _send(  # noqa: PLR0913 - one HTTP request's parts
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, JsonValue] | None = None,
        content: bytes | None = None,
        allow: tuple[int, ...] = (),
    ) -> httpx.Response:
        """`allow` names statuses this call reads itself; 404 is always one of them."""
        sent = await self.client.request(method, path, params=params, json=json, content=content)
        if sent.status_code >= BAD_REQUEST and sent.status_code not in (NOT_FOUND, *allow):
            raise _refused(sent, sent.content)
        return sent

    @staticmethod
    def _body(sent: httpx.Response) -> bytes:
        body = sent.content
        if len(body) > ARCHIVE_MAX:
            raise EngineError(sent.status_code, f"the answer is over {ARCHIVE_MAX} bytes")
        return body


class NameTakenError(Exception):
    """A 409 on create: the name is this operation key's earlier create, so it is that one."""


def _refused(sent: httpx.Response, body: bytes) -> EngineError:
    """The daemon's own message. Every error body is `{"message": "..."}`; anything else is
    carried as the bytes it was, so a failure is never hidden behind a parse failure."""
    try:
        message = wire.Failure.model_validate_json(body).message
    except ValidationError:
        message = body.decode("utf-8", "replace")[:512]
    return EngineError(sent.status_code, message)


def open_engine(make: MakeTransport) -> Engine:
    """This loop's client. Made without I/O: the socket is connected on the first request."""
    inner = make()
    return Engine(httpx.AsyncClient(base_url=BASE_URL, transport=FencedHttpx(inner)), inner)


async def close_engine(engine: Engine) -> None:
    """The client first: closing it ends every connection it holds, so an exec stream still
    reading fails rather than being cancelled mid-connect."""
    await close_all(
        [
            engine.client.aclose,
            lambda: drain(engine.pumps, _PUMP_GRACE_S),
            engine.inner.aclose,
        ]
    )


def label_of(operation_key: str) -> str:
    return f"threads.operation_key={operation_key}"


def argv_env(env: Mapping[str, str]) -> Sequence[str]:
    """The exec's `Env`: the carrier names posix.wrap built, which the wrapper hands to
    `env -i` inside the container. Nothing of the host's environment is added."""
    return [f"{name}={value}" for name, value in sorted(env.items())]

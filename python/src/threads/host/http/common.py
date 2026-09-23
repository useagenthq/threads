"""What every host API route shares: the caller authenticated as a principal (401 without one),
bodies parsed by the generated host-api models (400 on failure), the tenant's thread at the
requested branch, and Failure bodies with their status."""

from collections.abc import Awaitable, Callable
from typing import Final

from pydantic import BaseModel, JsonValue, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from threads.host.app import Host
from threads.log import BranchId, ParseError, Principal, ThreadId
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.thread.control import Controlled
from threads.thread.handle import Thread

_STATUS: Final = {
    "unauthenticated": 401,
    "unverified": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid_request": 400,
}

type Handler = Callable[[Request], Awaitable[Response]]
type Authenticated = Callable[[Request, Principal], Awaitable[Response]]
type OnThread = Callable[[Request, Principal, Thread], Awaitable[Response]]


def error(code: str, message: str) -> JSONResponse:
    """A Failure body with its status: 400, 401, 403, 404, else 409 for a domain code."""
    body: JsonValue = {"error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=_STATUS.get(code, 409))


def failed(failure: ParseError) -> JSONResponse:
    """A library failure as the route's error. The store's branch_not_found (absent, or another
    tenant's) is the API's not_found."""
    code = "not_found" if failure.code == "branch_not_found" else failure.code
    return error(code, failure.message)


def authenticated(host: Host, handle: Authenticated) -> Handler:
    async def route(request: Request) -> Response:
        principal = None if host.authenticate is None else await host.authenticate(request)
        if principal is None:
            return error("unauthenticated", "no authenticated principal")
        return await handle(request, principal)

    return route


async def body[M: BaseModel](request: Request, model: type[M]) -> M | JSONResponse:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as failure:
        return error("invalid_request", f"{failure.error_count()} invalid field(s)")


def on_thread(host: Host, handle: OnThread) -> Handler:
    """A /v1/threads/{thread_id} route: the tenant's thread at `?branch_id` (its main branch by
    default), which must belong to that thread; otherwise not_found."""

    async def route(request: Request, principal: Principal) -> Response:
        branch = request.query_params.get("branch_id")
        thread_id = ThreadId(request.path_params["thread_id"])
        at = None if branch is None else BranchId(branch)
        opened = await host.thread(principal, thread_id, at)
        if isinstance(opened, Err):
            return failed(opened.error)
        return await handle(request, principal, opened.value)

    return authenticated(host, route)


def appended(done: Controlled) -> Response:
    return failed(done.error) if isinstance(done, Err) else JSONResponse(to_json(done.value))

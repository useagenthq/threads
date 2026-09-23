"""The /v1/threads/{thread_id} routes: thin wrappers over the Thread handle's methods, each
called with the authenticated principal. A control that may unpark the thread resumes it."""

from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from threads._generated.host_api_v1 import (
    Answer,
    ApprovalDecision,
    BranchRef,
    ForkRequest,
    ModeChange,
    ParkedResolution,
    SettingsChange,
)
from threads.host.app import Host
from threads.host.http.common import OnThread, appended, body, error, failed, on_thread
from threads.log import CallId, Principal
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.thread.handle import Thread

if TYPE_CHECKING:
    from pydantic import JsonValue


def routes(host: Host) -> list[Route]:
    base = "/v1/threads/{thread_id}"
    table: list[tuple[str, str, OnThread]] = [
        ("GET", "/timeline", _timeline),
        ("GET", "/branches", _branches),
        ("GET", "/fork-points", _fork_points),
        ("POST", "/forks", _fork),
        ("GET", "/approvals", _approvals),
        ("POST", "/approvals/{challenge_id}", _decide(host)),
        ("POST", "/questions/{call_id}/answer", _answer(host)),
        ("POST", "/parked/{effect_key}/resolve", _resolve(host)),
        ("POST", "/cancel", _cancel(host)),
        ("POST", "/settings", _settings),
        ("POST", "/mode", _mode),
    ]
    return [Route(base + path, on_thread(host, handle), methods=[m]) for m, path, handle in table]


async def _timeline(_request: Request, _principal: Principal, thread: Thread) -> Response:
    timeline = await thread.timeline()
    if isinstance(timeline, Err):
        return failed(timeline.error)
    entries: list[JsonValue] = [
        {"event": to_json(e.event), "fork_point": e.fork_point} for e in timeline.value.entries
    ]
    body: JsonValue = {"thread_id": thread.id, "branch_id": thread.branch, "entries": entries}
    return JSONResponse(body)


async def _branches(_request: Request, _principal: Principal, thread: Thread) -> Response:
    listed: list[JsonValue] = [to_json(b) for b in await thread.branches()]
    return JSONResponse(listed)


async def _fork_points(_request: Request, _principal: Principal, thread: Thread) -> Response:
    points = await thread.fork_points()
    if isinstance(points, Err):
        return failed(points.error)
    listed: list[JsonValue] = [
        {
            "branch_id": p.branch_id,
            "seq": p.seq,
            "event_id": p.event_id,
            "snapshot": to_json(p.snapshot),
        }
        for p in points.value
    ]
    return JSONResponse(listed)


async def _fork(request: Request, _principal: Principal, thread: Thread) -> Response:
    asked = await body(request, ForkRequest)
    if isinstance(asked, JSONResponse):
        return asked
    if asked.mode == "stub":
        return error("invalid_request", "stub forks are not supported by this host yet")
    knowledge = "pinned" if asked.knowledge is MISSING else asked.knowledge
    forked = await thread.fork(asked.event_id, knowledge=knowledge)
    if isinstance(forked, Err):
        return failed(forked.error)
    child = BranchRef(thread_id=forked.value.id, branch_id=forked.value.branch)
    return JSONResponse(to_json(child), status_code=201)


async def _approvals(_request: Request, _principal: Principal, thread: Thread) -> Response:
    pending = await thread.pending_approvals()
    if isinstance(pending, Err):
        return failed(pending.error)
    listed: list[JsonValue] = [to_json(p) for p in pending.value]
    return JSONResponse(listed)


def _decide(host: Host) -> OnThread:
    async def handle(request: Request, principal: Principal, thread: Thread) -> Response:
        decision = await body(request, ApprovalDecision)
        if isinstance(decision, JSONResponse):
            return decision
        challenge = request.path_params["challenge_id"]
        if decision.decision == "grant":
            rule = None if decision.remember_rule is MISSING else decision.remember_rule
            done = await thread.approve(challenge, principal, remember_rule=rule)
        else:
            if decision.remember_rule is not MISSING:
                return error("invalid_request", "remember_rule goes with grant only")
            reason = None if decision.reason is MISSING else decision.reason
            done = await thread.deny(challenge, principal, reason=reason)
        await host.resume(thread)
        return appended(done)

    return handle


def _answer(host: Host) -> OnThread:
    async def handle(request: Request, principal: Principal, thread: Thread) -> Response:
        answer = await body(request, Answer)
        if isinstance(answer, JSONResponse):
            return answer
        call = CallId(request.path_params["call_id"])
        done = await thread.answer(call, answer.answer, principal)
        await host.resume(thread)
        return appended(done)

    return handle


def _resolve(host: Host) -> OnThread:
    async def handle(request: Request, principal: Principal, thread: Thread) -> Response:
        resolution = await body(request, ParkedResolution)
        if isinstance(resolution, JSONResponse):
            return resolution
        key = request.path_params["effect_key"]
        done = await thread.resolve_parked(key, resolution.resolution, principal)
        await host.resume(thread)
        return appended(done)

    return handle


def _cancel(host: Host) -> OnThread:
    async def handle(_request: Request, principal: Principal, thread: Thread) -> Response:
        done = await thread.cancel(principal)
        await host.resume(thread)
        return appended(done)

    return handle


async def _settings(request: Request, principal: Principal, thread: Thread) -> Response:
    change = await body(request, SettingsChange)
    if isinstance(change, JSONResponse):
        return change
    return appended(await thread.set_model(change, principal))


async def _mode(request: Request, principal: Principal, thread: Thread) -> Response:
    change = await body(request, ModeChange)
    if isinstance(change, JSONResponse):
        return change
    return appended(await thread.set_mode(change.mode, principal))

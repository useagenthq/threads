"""The crash-drill rig for the outbound A2A send.

A partner that records every request it was asked to send and can lose an answer on purpose, and a
branch whose only tools are a real remote's two. The counts are the point: a drill that cannot say
how many times we sent proves nothing about invariant 3.

This mirrors typescript/packages/a2a/test/outbound/ case for case."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from corpus import Clock
from kit import T0, USER, acquire, allow_all, open_store
from pydantic import JsonValue, TypeAdapter

from threads.a2a.outbound.tools import RemoteTool
from threads.a2a.protocol import A2A_VERSION, IDEMPOTENT_SEND
from threads.a2a.remote import A2aAuth, remote
from threads.log import BranchId, JsonObject, ThreadId, ToolSpec
from threads.log.digest import canonical_sha256
from threads.loop.drafts import draft
from threads.loop.model import LookupResult
from threads.loop.runtime import Runtime, serving
from threads.loop.scripted import scripted_model
from threads.loop.tools import Dispatched, Invocation, Prepared, Termination, prepared
from threads.permissions import Decision
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import Draft, SqliteStore, StoredEvent, Writer
from threads.store.lines import uuid7
from threads.web.guard import Target
from threads.web.http import Fence, Request, Response, WebError

TOOL: Final = "refund_desk"
CARD_URL: Final = "https://partner.example/.well-known/agent-card.json"
RPC_URL: Final = "https://partner.example/a2a/refunds"


class Crash(BaseException):
    """The process dying mid-dispatch: nothing catches it, as nothing catches a kill."""


@dataclass(frozen=True, slots=True)
class Recorded:
    method: str
    path: str
    body: str | None
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class Answer:
    """What one scripted `SendMessage` does: answer, lose the answer, or refuse the connection."""

    kind: Literal["task", "lost", "refused"]
    task: dict[str, JsonValue] | None = None
    code: str = ""


def task(task_id: str, state: str, text: str | None = None) -> dict[str, JsonValue]:
    status: dict[str, JsonValue] = {"state": state}
    if text is not None:
        status["message"] = {
            "messageId": f"m-{task_id}",
            "role": "ROLE_AGENT",
            "parts": [{"text": text}],
        }
    return {"id": task_id, "contextId": "ignored-by-us", "status": status}


def card(window_ms: int | None = None) -> dict[str, JsonValue]:
    capabilities: dict[str, JsonValue] = {"streaming": False}
    if window_ms is not None:
        capabilities["extensions"] = [{"uri": IDEMPOTENT_SEND, "params": {"window_ms": window_ms}}]
    return {
        "name": "refunds",
        "description": "The partner's refunds desk.",
        "version": "1.0.0",
        "capabilities": capabilities,
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
        "supportedInterfaces": [
            {"url": RPC_URL, "protocolBinding": "JSONRPC", "protocolVersion": A2A_VERSION}
        ],
    }


_JSON: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)


def _parsed(text_: str) -> JsonValue:
    return _JSON.validate_json(text_)


def _at(value: JsonValue, key: str) -> JsonValue:
    return value.get(key) if isinstance(value, dict) else None


def _str_at(value: JsonValue, key: str) -> str | None:
    found = _at(value, key)
    return found if isinstance(found, str) else None


def message_id_in(body: str) -> str:
    """The messageId the body carries: what a peer would deduplicate on."""
    parsed = _parsed(body)
    params = _at(parsed, "params")
    return _str_at(_at(params if params is not None else parsed, "message"), "messageId") or ""


@dataclass
class Partner:
    """A partner over the transport seam. `send` is set per drill."""

    answer: Answer = field(default_factory=lambda: Answer("task", task("t", "TASK_STATE_WORKING")))
    tasks: dict[str, dict[str, JsonValue]] = field(default_factory=dict[str, dict[str, JsonValue]])
    requests: list[Recorded] = field(default_factory=list[Recorded])
    hidden: bool = False
    """While true, ListTasks answers an empty page even though the task exists: the peer is still
    creating it, or has truncated its history. The case that must never settle a park."""
    crash_after_answer: bool = False
    """The process dies once the partner's answer is on the wire, before it can be recorded."""
    card_json: dict[str, JsonValue] = field(default_factory=card)

    def sends(self) -> list[Recorded]:
        return [r for r in self.requests if r.method == "SendMessage"]

    async def resolve(self, _host: str, _port: int) -> Sequence[str]:
        return ("93.184.216.34",)

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        assert await fence()
        assert max_bytes > 0
        body = None if request.body is None else request.body.decode()
        method = self._method(target.path, request.method, body)
        self.requests.append(Recorded(method, target.path, body, dict(request.headers)))
        if method == "card":
            return Ok(_json(self.card_json))
        if method == "SendMessage":
            return self._sent(body or "")
        if method == "ListTasks":
            listed: list[JsonValue] = [] if self.hidden else list(self.tasks.values())
            page: dict[str, JsonValue] = {
                "tasks": listed,
                "nextPageToken": "",
                "pageSize": 50,
                "totalSize": len(listed),
            }
            return Ok(_json(page))
        if method == "GetTask":
            found = next(iter(self.tasks.values()), None)
            return Ok(_json({"task": found}))
        return Ok(_json({"code": -32601, "message": method}, status=404))

    def _method(self, path: str, verb: str, body: str | None) -> str:
        if path.endswith("agent-card.json"):
            return "card"
        if verb == "GET":
            return "ListTasks" if path.split("?", maxsplit=1)[0].endswith("/tasks") else "GetTask"
        return _str_at(_parsed(body or "{}"), "method") or "unknown"

    def _sent(self, body: str) -> Ok[Response] | Err[WebError]:
        answer = self.answer
        if answer.kind == "refused":
            return Err(WebError("unavailable", f"connect refused: {answer.code}", sent=False))
        held = answer.task or {}
        task_id = held["id"]
        assert isinstance(task_id, str)
        seen = self.tasks.get(task_id, {}).get("history")
        history: list[JsonValue] = list(seen) if isinstance(seen, list) else []
        history.append(
            {
                "messageId": message_id_in(body),
                "role": "ROLE_USER",
                "parts": [{"text": "what we asked"}],
            }
        )
        # The task exists at the partner either way: a lost answer loses only our knowledge of it.
        self.tasks[task_id] = {**held, "history": history}
        if self.crash_after_answer:
            raise Crash("killed with the answer on the wire")
        if answer.kind == "lost":
            return Err(WebError("unavailable", f"reset: {answer.code}", sent=True))
        return Ok(_json({"task": self.tasks[task_id]}))


def _json(body: JsonValue, status: int = 200) -> Response:
    return Response(status, {"content-type": "application/json"}, json.dumps(body).encode())


@dataclass
class Remotes:
    """Routes the remote's two tool names to their own runner, as the agents layer does."""

    tools: dict[str, RemoteTool]
    crash_before_send: bool = False
    """The process dies after the durable begin and before any byte leaves."""
    crash_in_begin: bool = False
    """The process dies while preparing, so the call is recorded and allowed but never began."""

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        return self.tools[spec.name].invalid(spec, input)

    async def begin(self, call: Invocation) -> Prepared:
        if self.crash_in_begin:
            raise Crash("killed before anything of the attempt was durable")
        return await prepared(self.tools[call.spec.name], call)

    async def dispatch(self, call: Invocation) -> Dispatched:
        if self.crash_before_send:
            raise Crash("killed after the begin, before the request left")
        return await self.tools[call.spec.name].dispatch(call)

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return await self.tools[call.spec.name].lookup(call)

    async def terminate(self, call: Invocation) -> Termination:
        return await self.tools[call.spec.name].terminate(call)

    def provider_now(self) -> int | None:
        return None


def use(name: str = TOOL, **input: JsonValue) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "call_1", "name": name, "input": input}
    usage: JsonValue = {"input_tokens": 5, "output_tokens": 1}
    return {"content": [part], "stop_reason": "tool_use", "usage": usage}


def text(reply: str) -> JsonValue:
    usage: JsonValue = {"input_tokens": 5, "output_tokens": 1}
    content: JsonValue = [{"type": "text", "text": reply}]
    return {"content": content, "stop_reason": "end_turn", "usage": usage}


@dataclass
class Drill:
    """One branch, its partner and its clock, with a restart that takes a fresh writer."""

    store: SqliteStore
    branch: BranchId
    clock: Clock
    partner: Partner
    runner: Remotes
    specs: tuple[ToolSpec, ...]
    rt: Runtime

    async def restart(self, after_ms: int = 60_000) -> Runtime:
        """A new process over the same log: the clock moves past the dead owner's lease first."""
        self.clock.now += after_ms
        writer = await acquire(self.store, self.branch, "restarted", self.clock)
        self.rt = _runtime(self.store, writer, self.runner, self.clock)
        return self.rt

    def kinds(self) -> list[str]:
        return [e.type for e in self.rt.events]

    def last(self, kind: str) -> StoredEvent | None:
        found = [e for e in self.rt.events if e.type == kind]
        return found[-1] if found else None


def ask_all(_fold: object, _call: object, _spec: object) -> Decision:
    """Every call needs an approver: how a drill reaches a call that never began."""
    return Decision("ask", "policy", "test_ask")


def cancelled() -> Draft:
    """A cancel another process recorded, with the principal its schema requires."""
    return replace(draft("cancel_requested", {"scope": "turn"}), actor=USER)


def tools_changed(specs: Sequence[ToolSpec], without: str) -> Draft:
    """A tools_changed that drops `without` from the set, hashed as the rule requires."""
    kept = [to_json(s) for s in specs if s.name != without]
    listed: JsonValue = kept
    hashed = canonical_sha256(listed)
    assert isinstance(hashed, Ok)
    data: dict[str, JsonValue] = {"tools": kept, "tools_hash": hashed.value}
    return draft("tools_changed", data)


def _runtime(store: SqliteStore, writer: Writer, runner: Remotes, clock: Clock) -> Runtime:
    model = scripted_model({"responses": []})
    return Runtime(store, writer, serving(model), runner, allow_all, clock, clock.wait_until)


async def drill(
    partner_: Partner,
    responses: Sequence[JsonValue] | None = None,
    *,
    timeout_ms: int = 1,
    auth: A2aAuth | None = None,
) -> Drill:
    """A branch pinned with the remote's two tools and a model that calls the send once.
    `timeout_ms` is tiny so a follow never waits on real time."""
    clock = Clock(T0)
    r = remote(
        "refunds",
        CARD_URL,
        timeout_ms=timeout_ms,
        transport=partner_,
        resolve=partner_.resolve,
        auth=auth,
    )
    tools = r.tools(name=TOOL, description="Ask the refunds desk.")
    specs = tuple(t.spec() for t in tools)
    runner = Remotes({t.name: t for t in tools})
    store = await open_store()
    thread, branch = ThreadId(uuid7(clock())), BranchId(uuid7(clock()))
    assert await store.create(thread, branch, clock()) == Ok(None)
    writer = await acquire(store, branch, "first", clock)
    model = scripted_model(
        {
            "responses": list(responses)
            if responses is not None
            else [use(message="ask"), text("ok")]
        }
    )
    rt = Runtime(store, writer, serving(model), runner, allow_all, clock, clock.wait_until)
    started: dict[str, JsonValue] = {
        "agent_name": "test",
        "config_hash": "0" * 64,
        "instructions": "Test.",
        "model": to_json(model.info.model),
        "model_params": dict(model.info.params),
        "adapter": to_json(model.info.adapter),
        "tools": [to_json(s) for s in specs],
    }
    user = replace(draft("user_input", {"source": "api", "text": "go"}), actor=USER)
    assert isinstance(await rt.append(draft("thread_started", started), user), Ok)
    return Drill(store, branch, clock, partner_, runner, specs, rt)

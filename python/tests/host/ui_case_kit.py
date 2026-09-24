"""The `ui` conformance runner's driver (spec/conformance/README.md, "ui"): the host's own session,
hub and listener, moved by the case's timeline instead of a route, over the log the case
imports."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from corpus import import_and_read, load, now_of
from pydantic import JsonValue, TypeAdapter

from threads._generated.host_api_v1 import AgUiResumeEntry
from threads.agents.store import sqlite
from threads.host.outcome import logged, run_start
from threads.host.ui.ag_ui_resume import resume_conflicts
from threads.host.ui.closing import RunIds, ending
from threads.host.ui.connection import Cursor
from threads.host.ui.frame import Frame, Protocol
from threads.host.ui.hub import LiveHub
from threads.host.ui.listener import LiveListener
from threads.host.ui.live import Delta
from threads.host.ui.session import SessionPlan, UiSession
from threads.log import Event, EventId, ParkAddress, ParkedEvent, ResumedEvent, ThreadId
from threads.log.jcs import canonicalize
from threads.result import Ok
from threads.thread.handle import Thread

_JSON: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class Served:
    frames: Sequence[Frame]
    done: bool
    """The stream ended cleanly (the AI SDK then sends [DONE]); False: a broken live part."""


@dataclass(frozen=True, slots=True)
class Case:
    path: Path
    input: Mapping[str, JsonValue]
    now: int
    events: Sequence[Event]

    @property
    def protocol(self) -> Protocol:
        return "ai-sdk" if self.input["protocol"] == "ai-sdk" else "ag-ui"

    @property
    def run_id(self) -> EventId:
        return EventId(str(self.input["run_id"]))


def loaded(path: Path) -> Case:
    meta = load(path, "case.json")
    raw = meta["input"]
    assert isinstance(raw, dict)
    now = now_of(meta)
    read = asyncio.run(import_and_read(path, (path / "log.jsonl").read_bytes(), now))
    assert isinstance(read, Ok), read
    return Case(path, raw, now, read.value.fold.events)


def parks(events: Sequence[Event]) -> list[ParkAddress]:
    open_: list[ParkAddress] = []
    for e in events:
        if isinstance(e, ParkedEvent):
            open_.append(e.data.address)
        elif isinstance(e, ResumedEvent) and e.data.address in open_:
            open_.remove(e.data.address)
    return open_


def plan(case: Case, without_after: bool = False) -> SessionPlan:
    raw = case.input
    resume = [AgUiResumeEntry.model_validate(r) for r in _list(raw.get("resume"))]
    extra = resume_conflicts(case.events, parks(case.events), resume, case.now)
    assert isinstance(extra, Ok), extra
    first = case.events[0]
    ids = raw.get("ids")
    run_ids = (
        RunIds(str(ids["threadId"]), str(ids["runId"]))
        if isinstance(ids, dict)
        else RunIds(first.thread_id, case.run_id)
    )
    receipts = {k: str(v) for k, v in _dict(raw.get("receipts")).items()}
    after = raw.get("after")
    cursor = None if without_after or not isinstance(after, str) else after.split(":")
    replay = "head" if raw.get("replay") is True else None
    if cursor is not None and case.protocol == "ag-ui":
        return SessionPlan(
            case.protocol, case.run_id, run_ids, None, int(cursor[0]), extra.value, receipts
        )
    at = None if cursor is None else Cursor(int(cursor[0]), int(cursor[1]))
    return SessionPlan(case.protocol, case.run_id, run_ids, at, replay, extra.value, receipts)


def _list(v: JsonValue) -> list[JsonValue]:
    return v if isinstance(v, list) else []


def _dict(v: JsonValue) -> dict[str, JsonValue]:
    return v if isinstance(v, dict) else {}


def serve(case: Case, without_after: bool = False) -> Served:
    """One connection over the case's timeline, as the README's runner steps say."""
    live = case.input.get("live")
    last = case.events[-1].seq
    whole: list[JsonValue] = [{"register": True}, {"commit": last}]
    steps = _list(live) if isinstance(live, list) else whole
    thread_id = case.events[0].thread_id
    thread = Thread(ThreadId(thread_id), case.events[-1].branch_id, sqlite(":memory:"))
    hub = LiveHub()
    out: list[Frame] = []
    head = 0
    listener: LiveListener | None = None
    session: UiSession | None = None
    p = plan(case, without_after)
    for raw in steps:
        step = _dict(raw)
        if "register" in step:
            listener = LiveListener(hub if isinstance(live, list) else None, "acme", thread_id)
            continue
        head = _step(case, hub, thread_id, step, head)
        if listener is None:
            continue
        visible = [e for e in case.events if e.seq <= head]
        if session is None:
            session = UiSession(p, visible, listener.missed)
            out.extend(session.opening(visible))
        out.extend(session.deltas(listener.take()))
        read = session.read(visible)
        out.extend(read.frames)
        if read.broken is not None:
            return Served(out, done=False)
        start = run_start(visible, case.run_id)
        outcome = None if start is None else logged(visible, start, parks(visible), thread)
        if outcome is not None:
            out.extend(session.close(ending(outcome)))
            return Served(out, done=True)
    raise AssertionError("a ui case's timeline ends with the run's outcome")


def _step(case: Case, hub: LiveHub, thread: str, step: dict[str, JsonValue], head: int) -> int:
    delta = step.get("delta")
    if isinstance(delta, dict):
        part = delta["part"]
        assert isinstance(part, int)
        hub.delta("acme", thread, Delta(str(delta["request_event_id"]), part, str(delta["text"])))
        return head
    commit = step["commit"]
    assert isinstance(commit, int)
    for e in case.events:
        if head < e.seq <= commit:
            hub.appended("acme", thread, e)
    return commit


def line(f: Frame | str) -> str:
    value: JsonValue = {"data": f} if isinstance(f, str) else _frame(f)
    text = canonicalize(value)
    assert isinstance(text, Ok)
    return text.value


def _frame(f: Frame) -> JsonValue:
    data: JsonValue = dict(f.data)
    return {"data": data} if f.id is None else {"id": f.id, "data": data}


def expected(case: Case) -> list[JsonValue]:
    messages = _JSON.validate_json((case.path / "expected.json").read_bytes())["messages"]
    assert isinstance(messages, list)
    return messages

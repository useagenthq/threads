"""One thread per schedule, replayed from the shared vector
spec/conformance/vectors/schedule-threads.json (the TypeScript host suite replays the same
file)."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from threads import agent, scripted_model, sqlite
from threads.agents.store import Store, now_ms, open_store
from threads.host import Schedule
from threads.host.runs import Runner
from threads.host.schedules import Scheduler
from threads.log import (
    BranchId,
    ScheduleFiredEvent,
    ScheduleSkippedEvent,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.result import Ok
from threads.store import Draft, SqliteStore, Writer
from threads.store.retention import delete_thread
from threads.store.sql import text_of

VECTOR = Path(__file__).resolve().parents[3] / "spec/conformance/vectors/schedule-threads.json"
REPLY: JsonValue = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
OPERATOR: JsonValue = {"issuer": "api", "tenant": "local", "subject": "operator"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScheduleJson(_Strict):
    id: str
    agent: str
    cron: str
    input: str


class Expected(_Strict):
    log: list[str]
    rows: list[tuple[str, str, str | None]]
    threads: int | None = None
    inputs: list[str] | None = None
    one_hold: list[str] | None = None


class Start(_Strict):
    start: str
    at: str
    tenant: str = "local"
    schedules: list[ScheduleJson] | None = None
    agents: list[str] = ["bot"]
    instructions: str | None = None


class Tick(_Strict):
    tick: str
    at: str


class Race(_Strict):
    race: list[str]
    at: str


class Hold(_Strict):
    hold: Literal["run", "outbound"]


class Release(_Strict):
    release: Literal["done", "crash"]


class Delete(_Strict):
    delete: Literal[True]


class Expect(_Strict):
    expect: dict[str, Expected]


type Step = Start | Tick | Race | Hold | Release | Delete | Expect


class Case(_Strict):
    name: str
    note: str
    steps: list[Step]


class Vector(_Strict):
    description: str
    schedule: ScheduleJson
    cases: list[Case]


vector = Vector.model_validate_json(VECTOR.read_text(encoding="utf-8"))


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class Running:
    runner: Runner
    scheduler: Scheduler
    started_at: int


@dataclass(slots=True)
class Held:
    writer: Writer
    kind: Literal["run", "outbound"]


class Replay:
    def __init__(self) -> None:
        self.store: Store = sqlite(":memory:")
        self.schedulers: dict[str, Running] = {}
        self.last: Running | None = None
        self.held: Held | None = None

    async def step(self, step: Step) -> None:
        match step:
            case Start():
                return self.start(step)
            case Tick():
                return await self.tick([step.tick], step.at)
            case Race():
                return await self.tick(step.race, step.at)
            case Hold():
                return await self.hold(step.hold)
            case Release():
                return await self.release(step.release)
            case Delete():
                return await self.delete()
            case Expect():
                for tenant, want in step.expect.items():
                    assert await self.observe(tenant, want) == want, tenant

    def start(self, step: Start) -> None:
        bots = {
            key: agent(
                name=key,
                model=scripted_model({"responses": [REPLY] * 12}),
                instructions=step.instructions or "",
            )
            for key in step.agents
        }
        runner = Runner(self.store, bots, {})
        given = vector.schedule if step.schedules is None else None
        chosen = [given] if given is not None else step.schedules or []
        schedules = [Schedule(s.id, s.agent, s.cron, s.input) for s in chosen]
        made = Running(runner, Scheduler(runner, schedules, tenant=step.tenant), _ms(step.at))
        self.schedulers[step.start] = self.last = made

    async def tick(self, names: list[str], at: str) -> None:
        ticking = [self.schedulers[n] for n in names]
        await asyncio.gather(*(s.scheduler.tick(s.started_at, _ms(at)) for s in ticking))
        for s in ticking:
            await s.runner.settled()

    async def hold(self, kind: Literal["run", "outbound"]) -> None:
        sq = await open_store(self.store)
        found = await self.thread("local")
        assert found is not None, "no schedule thread to hold"
        taken = await sq.acquire(found[1], "outside", now_ms)
        assert isinstance(taken, Ok)
        if kind == "run":
            actor: dict[str, JsonValue] = {"kind": "user", "principal": OPERATOR}
            busy = Draft("user_input", {"source": "api", "text": "Busy."}, actor)
            assert isinstance(await taken.value.append([busy]), Ok)
        self.held = Held(taken.value, kind)

    async def release(self, how: Literal["done", "crash"]) -> None:
        held = self.held
        assert held is not None, "nothing is held"
        await held.writer.release()
        self.held = None
        if how == "crash" or held.kind == "outbound":
            return
        found, last = await self.thread("local"), self.last
        assert found is not None, "no schedule thread"
        assert last is not None, "no scheduler to finish the run"
        await last.runner.resume(self.store, *found)
        await last.runner.settled()

    async def delete(self) -> None:
        sq = await open_store(self.store)
        found = await self.thread("local")
        assert found is not None, "no schedule thread to delete"
        await sq.run(lambda c: delete_thread(c, "local", found[0], now_ms()))

    async def observe(self, tenant: str, want: Expected) -> Expected:
        sq = await open_store(self.store)
        rows: list[tuple[int, str, str | None]] = await sq.run(
            lambda c: c.execute(
                "SELECT occurrence_at, state, reason FROM schedule_occurrences"
                " WHERE tenant_id = ? ORDER BY occurrence_at",
                (tenant,),
            ).fetchall()
        )
        ((count,),) = await sq.run(
            lambda c: c.execute(
                "SELECT count(*) FROM threads WHERE tenant_id = ?", (tenant,)
            ).fetchall()
        )
        events = await self.events(tenant)
        if events:
            # The schedule's thread opens with its one thread_started.
            assert isinstance(events[0], ThreadStartedEvent)
            assert sum(isinstance(e, ThreadStartedEvent) for e in events) == 1
        logged = [
            (
                f"fired {e.data.occurrence_id}"
                if isinstance(e, ScheduleFiredEvent)
                else f"skipped {e.data.reason} {e.data.occurrence_id}",
                e.epoch,
            )
            for e in events
            if isinstance(e, ScheduleFiredEvent | ScheduleSkippedEvent)
        ]
        held = {epoch for line, epoch in logged if line in (want.one_hold or [])}
        inputs = [
            e.data.text
            for e in events
            if isinstance(e, UserInputEvent)
            and e.data.source == "schedule"
            and isinstance(e.data.text, str)
        ]
        return Expected(
            log=[line for line, _ in logged],
            rows=[(_iso(at), state, reason) for at, state, reason in rows],
            threads=None if want.threads is None else count,
            inputs=None if want.inputs is None else inputs,
            one_hold=None
            if want.one_hold is None
            else want.one_hold
            if len(held) == 1
            else [str(e) for e in held],
        )

    async def events(self, tenant: str) -> list[object]:
        found = await self.thread(tenant)
        if found is None:
            return []
        sq = (await open_store(self.store)).scoped(tenant)
        read = await sq.read(found[1], now_ms())
        assert isinstance(read, Ok)
        return list(read.value.fold.events)

    async def thread(self, tenant: str) -> tuple[ThreadId, BranchId] | None:
        sq: SqliteStore = (await open_store(self.store)).scoped(tenant)
        rows: list[tuple[object]] = await sq.run(
            lambda c: c.execute(
                "SELECT thread_id FROM schedule_threads WHERE tenant_id = ? AND schedule_id = ?",
                (tenant, vector.schedule.id),
            ).fetchall()
        )
        if not rows:
            return None
        thread = ThreadId(text_of(rows[0][0]))
        root = await sq.root(thread)
        return None if not isinstance(root, Ok) else (thread, root.value)

    async def stop(self) -> None:
        for s in self.schedulers.values():
            await s.runner.stop()


@pytest.mark.parametrize("case", vector.cases, ids=lambda c: c.name)
def test_schedule_threads(case: Case) -> None:
    async def main() -> None:
        replay = Replay()
        try:
            for step in case.steps:
                await replay.step(step)
        finally:
            await replay.stop()

    asyncio.run(main())

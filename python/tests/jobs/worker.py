"""A crash-drill host process (F9.6, F10.5).

The drills in this folder run it as a real child process on a shared SQLite store and kill it
with SIGKILL. `python worker.py <role> <dir>`:

- `serve`: a host with one fake channel. With `DRILL_WEBHOOK=1` it delivers the drill's one
  webhook (a redelivery after a restart). It answers "done" and the host sends that reply as a
  channel_send effect. It stops once the conversation settles: replied, or parked. With
  `DRILL_GO=1` it starts only once `<dir>/go` exists, so two of them race.
- `schedule`: fires every occurrence in `DRILL_OCCURRENCES` of one schedule once `<dir>/go`
  exists, then waits until each occurrence's run has ended, in whichever process ran it.

At the point named by `DRILL_STOP_AT` the process prints `at <point>` and blocks its event loop,
lease renewal included, until the parent kills it or creates `<dir>/release`. Every send, refusal
and model request is recorded in `<dir>` with the pid that made it.
"""

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import JsonValue, TypeAdapter

from threads import agent, extension, scripted_model, sqlite
from threads.agents.context import RunContext
from threads.agents.store import open_store, scoped
from threads.hooks.types import ModelGate
from threads.host import (
    ChannelCapabilities,
    DeliveryOutcome,
    Inbound,
    RawRequest,
    RawResponse,
    Schedule,
    Sent,
    VerifiedDelivery,
    host,
)
from threads.host.runs import Runner
from threads.host.schedules import Scheduler
from threads.log import Event, JsonObject, ParseError, Principal, TurnCompletedEvent
from threads.loop.guard import block_model_requests
from threads.loop.model import Found, LookupResult, LookupUnknown, ModelRequest, NotFound
from threads.memory.fence import check
from threads.reduce import Fold
from threads.reduce.state import ReducedState
from threads.result import Err, Ok
from threads.secrets import Secret
from threads.store import SqliteStore

TEAM: Final = "T1"
USER: Final = Principal(issuer="fake:T1", tenant=TEAM, subject="U1")
DONE: Final[JsonValue] = {
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
SETTLE_S: Final = 20.0
_ITEMS: TypeAdapter[list[Inbound]] = TypeAdapter(list[Inbound])
_OCCURRENCES: TypeAdapter[list[int]] = TypeAdapter(list[int])


def reached(point: str, where: Path) -> None:
    """A scripted point: blocks the whole process, event loop included, like a stalled host."""
    if os.environ.get("DRILL_STOP_AT") != point:
        return
    print(f"at {point}", flush=True)
    while not (where / "release").exists():
        time.sleep(0.01)


async def started(where: Path) -> None:
    """The start barrier of racing processes: `<dir>/go`."""
    while not (where / "go").exists():
        await asyncio.sleep(0.001)


def record(path: Path, row: Mapping[str, JsonValue]) -> None:
    """One durable line: a recorded send must survive this process being killed."""
    with path.open("a") as out:
        out.write(json.dumps({**row, "pid": os.getpid()}) + "\n")
        out.flush()
        os.fsync(out.fileno())


def rows(path: Path) -> list[dict[str, JsonValue]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def webhook() -> RawRequest:
    item: JsonValue = {
        "kind": "message",
        "principal": USER.model_dump(),
        "address": "C1",
        "item_key": "m1",
        "content": "hello",
    }
    return RawRequest({"delivery": "d1"}, json.dumps([item]).encode())


@dataclass
class FileChannel:
    """A fake provider whose sends are lines of `<dir>/sends.jsonl`. Its transport checks the
    run's fence right before the line is written, as a real adapter's HTTP transport does."""

    where: Path
    lookups: str
    """`final`: a lookup reads the sends; `unknown`: the provider can't say; `none`: no lookup."""
    agent: str = "bot"
    limits: Mapping[str, int] = field(default_factory=dict[str, int])
    secrets: Mapping[str, Secret] = field(default_factory=dict[str, Secret])

    @property
    def capabilities(self) -> ChannelCapabilities:
        lookup = "none" if self.lookups == "none" else "final"
        return ChannelCapabilities(lookup, False, False, False, False)

    def verify(self, raw: RawRequest) -> Ok[VerifiedDelivery] | Err[ParseError]:
        return Ok(VerifiedDelivery(TEAM, TEAM, raw.headers["delivery"]))

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]:
        return Ok(_ITEMS.validate_json(raw.body))

    def ack(self, raw: RawRequest) -> RawResponse:
        return RawResponse(200, {}, b"ok")

    def render(self, event: Event) -> Sequence[JsonObject]:
        if event.type != "model_response":
            return ()
        return ({"text": "".join(p.text for p in event.data.content if p.type == "text")},)

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        reached("effect_begin", self.where)
        try:
            await check()
        except Exception:
            record(self.where / "refused.jsonl", {"key": effect_key})
            raise
        record(self.where / "sends.jsonl", {"key": effect_key, "text": op["text"]})
        # The provider has it; its receipt is not yet the host's.
        reached("sent", self.where)
        return Sent(f"ref-{effect_key}")

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        if self.lookups == "unknown":
            return LookupUnknown("the provider did not answer")
        sent = any(r["key"] == effect_key for r in rows(self.where / "sends.jsonl"))
        return Found(f"ref-{effect_key}") if sent else NotFound()


async def read(sq: SqliteStore) -> Fold | None:
    """The drill conversation's branch, read back from the log; None before it has one."""
    found = await sq.tables.inbox_rows()
    if not found:
        return None
    root = await sq.root(found[0].thread_id)
    log = None if isinstance(root, Err) else await sq.read(root.value, 0)
    return log.value.fold if isinstance(log, Ok) else None


def settled(fold: Fold | None) -> bool:
    """Replied (the host's reply to the final response has its result) or parked."""
    if fold is None:
        return False
    replies = [c for c in fold.calls if c.startswith("send_")]
    return bool(fold.parked) or (bool(replies) and all(c in fold.results for c in replies))


async def serve(where: Path) -> None:
    if os.environ.get("DRILL_GO") == "1":
        await started(where)
    store = sqlite(str(where))
    before = await read(await open_store(scoped(store, TEAM)))
    # A restart answers only what the log has not: the script is the model's, not the process's.
    model = scripted_model({"responses": [DONE] if before is None or not before.responses else []})

    def played(_request: ModelRequest) -> None:
        record(where / "model.jsonl", {})
        reached("model_request", where)

    model.before_send = played

    async def gate(_state: ReducedState, _ctx: RunContext[None]) -> ModelGate:
        # before_model runs once the turn's user_input is durable, before its model_request.
        reached("user_input", where)
        return {"decision": "proceed"}

    async def committed(_event: Event) -> None:
        # An observer is told of an event after its append: the reply's commit is durable.
        reached("effect_commit", where)

    drill = extension(name="drill", hooks={"before_model": gate}, on={"effect_commit": committed})
    bot = agent(model=model, extensions=[drill])
    channel = FileChannel(where, os.environ.get("DRILL_LOOKUP", "final"))
    async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as served:
        if os.environ.get("DRILL_WEBHOOK") == "1":
            answer = await served.receive("fake", webhook())
            assert isinstance(answer, Ok), answer
            record(where / "acks.jsonl", {"status": answer.value.status})
            reached("webhook_ack", where)
        conversation = await open_store(scoped(store, TEAM))
        await until(lambda: _settled(conversation))


async def _settled(sq: SqliteStore) -> bool:
    return settled(await read(sq))


async def until(probe: Callable[[], Awaitable[bool]]) -> None:
    deadline = time.monotonic() + SETTLE_S
    while not await probe():
        if time.monotonic() > deadline:
            raise AssertionError("the drill never settled")
        await asyncio.sleep(0.02)


async def schedule(where: Path) -> None:
    occurrences = _OCCURRENCES.validate_json(os.environ["DRILL_OCCURRENCES"])
    store = sqlite(str(where))
    answers: list[JsonValue] = [DONE] * len(occurrences)
    model = scripted_model({"responses": answers})
    model.before_send = lambda _r: record(where / "model.jsonl", {})
    runner = Runner(store, {"bot": agent(model=model)}, {})
    tick = Schedule(id="tick", agent="bot", cron="* * * * *", input="Tick.")
    scheduler = Scheduler(runner, [tick])
    await started(where)
    for at in occurrences:
        if await scheduler.fire(tick, at):
            record(where / "claims.jsonl", {"at": at})
    sq = await open_store(store)
    await until(lambda: _ended(sq, len(occurrences)))
    await runner.stop()


async def _ended(sq: SqliteStore, count: int) -> bool:
    """Every occurrence is claimed and its run's turn has completed."""
    claimed = await sq.run(
        lambda c: c.execute("SELECT thread_id FROM schedule_occurrences").fetchall()
    )
    if len(claimed) < count:
        return False
    for (thread,) in claimed:
        root = await sq.root(thread)
        log = None if isinstance(root, Err) else await sq.read(root.value, 0)
        if not isinstance(log, Ok):
            return False
        if not any(isinstance(e, TurnCompletedEvent) for e in log.value.fold.events):
            return False
    return True


if __name__ == "__main__":
    block_model_requests()
    role, where = sys.argv[1], Path(sys.argv[2])
    asyncio.run(serve(where) if role == "serve" else schedule(where))

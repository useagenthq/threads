"""A crash-drill host process (F9.6, F10.5).

The drills in this folder run it as a real child process on a shared SQLite store and kill it
with SIGKILL. `python worker.py <role> <dir>`:

- `serve`: a host with one fake channel. With `DRILL_WEBHOOK=1` it delivers the drill's one
  webhook (a redelivery after a restart). It answers "done" and the host sends that reply as a
  channel_send effect. It stops once the conversation settles: replied, or parked. With
  `DRILL_GO=1` it starts only once `<dir>/go` exists, so two of them race.
- `schedule`: ticks one schedule through every occurrence in `DRILL_OCCURRENCES` once
  `<dir>/go` exists, then waits until each occurrence was decided and no run is in flight, in
  whichever process ran it.

At the point named by `DRILL_STOP_AT` the process prints `at <point>` and blocks its event loop,
lease renewal included, until the parent kills it or creates `<dir>/release`. Every send, refusal
and model request is recorded in `<dir>` with the pid that made it.
"""

import asyncio
import faulthandler
import json
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import CoroutineType
from typing import Final

from jobs.stores import drill_store
from pydantic import JsonValue, TypeAdapter

from threads import agent, extension, scripted_model
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
from threads.log import EffectCommitEvent, Event, JsonObject, ParseError, Principal
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

    def render_text(self, text: str) -> Sequence[JsonObject]:
        return ({"text": text},)

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
    store = drill_store(where)
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

    drill = extension(name="drill", hooks={"before_model": gate})
    bot = agent(model=model, extensions=[drill])
    channel = FileChannel(where, os.environ.get("DRILL_LOOKUP", "final"))
    async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as served:
        if os.environ.get("DRILL_WEBHOOK") == "1":
            answer = await served.receive("fake", webhook())
            assert isinstance(answer, Ok), answer
            record(where / "acks.jsonl", {"status": answer.value.status})
            reached("webhook_ack", where)
        conversation = await open_store(scoped(store, TEAM))
        await until(lambda: _settled(conversation, where))


async def _settled(sq: SqliteStore, where: Path) -> bool:
    fold = await read(sq)
    # The host appends a reply's effect_commit with its tool_result, outside any run's
    # observers: seen in the log, the commit is durable. An observer is background delivery
    # (it may lag past the end of the run, by contract), so it is no kill barrier. The same
    # as TypeScript's test/jobs/worker.ts.
    if fold is not None and any(isinstance(e, EffectCommitEvent) for e in fold.events):
        reached("effect_commit", where)
    return settled(fold)


async def until(probe: Callable[[], Awaitable[bool]]) -> None:
    deadline = time.monotonic() + SETTLE_S
    while not await probe():
        if time.monotonic() > deadline:
            raise AssertionError("the drill never settled")
        await asyncio.sleep(0.02)


async def schedule(where: Path) -> None:
    occurrences = _OCCURRENCES.validate_json(os.environ["DRILL_OCCURRENCES"])
    store = drill_store(where)
    answers: list[JsonValue] = [DONE] * len(occurrences)
    model = scripted_model({"responses": answers})
    model.before_send = lambda _r: record(where / "model.jsonl", {})
    runner = Runner(store, {"bot": agent(name="bot", model=model)}, {})
    tick = Schedule(id="tick", agent="bot", cron="* * * * *", input="Tick.")
    scheduler = Scheduler(runner, [tick])
    await started(where)
    sq = await open_store(store)
    started_at = occurrences[0] - 1
    now = started_at
    # Like a host's tick loop: every pass decides what is due at `now` and resumes a run whose
    # input is durable but that never went. Each occurrence waits out the last one's run, so few
    # are skipped as overlaps.
    for at in occurrences:
        await until(partial(_ticked, scheduler, sq, started_at, now, None))
        now = at + 1
    await until(partial(_ticked, scheduler, sq, started_at, now, len(occurrences)))
    await runner.stop()


async def _ticked(
    scheduler: Scheduler, sq: SqliteStore, started_at: int, now: int, count: int | None
) -> bool:
    """One pass; done once no turn is open and, with `count`, that many occurrences are
    decided."""
    await scheduler.tick(started_at, now)
    decided = await sq.run(
        lambda c: c.execute(
            "SELECT count(*) FROM schedule_occurrences WHERE state <> 'pending'"
        ).fetchone()
    )
    if count is not None and decided != (count,):
        return False
    for thread in await sq.tables.schedules.threads():
        root = await sq.root(thread)
        log = None if isinstance(root, Err) else await sq.read(root.value, 0)
        if not isinstance(log, Ok) or log.value.fold.in_turn:
            return False
    return True


def dump() -> None:
    """On SIGUSR1, from a drill that ran out of time: where every thread and task is."""
    faulthandler.dump_traceback(all_threads=True)
    for task in asyncio.all_tasks():
        print(f"task {task.get_name()}:", file=sys.stderr)
        # Down the await chain: a task's own stack stops at its outermost coroutine.
        awaited: object = task.get_coro()
        while isinstance(awaited, CoroutineType):
            frame = awaited.cr_frame
            if frame is not None:
                print(f"  {frame.f_code.co_filename}:{frame.f_lineno}", file=sys.stderr)
            awaited = awaited.cr_await
    sys.stderr.flush()


async def main(role: str, where: Path) -> None:
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, dump)
    await (serve(where) if role == "serve" else schedule(where))


if __name__ == "__main__":
    block_model_requests()
    asyncio.run(main(sys.argv[1], Path(sys.argv[2])))

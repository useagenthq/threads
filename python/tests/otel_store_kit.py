"""Shared by the exporter tests: a scripted tool-loop agent on a real store, and the store's
own rows (cursors, losses) read back."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, JsonValue

from threads import Agent, RunContext, Store, agent, scripted_model, tool
from threads.agents.store import open_store
from threads.log import BranchId, Permissions
from threads.log.digest import sha256_hex
from threads.result import Ok
from threads.store.conn import Conn
from threads.store.lines import head_line
from threads.store.verify import verify_export

CASES = Path(__file__).resolve().parents[2] / "spec" / "otel" / "cases"
NOW = 1_790_000_060_000
USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALLOW = Permissions(
    mode="default",
    allow=["echo"],
    ask=[],
    deny=[],
    protected_paths=[".git/**"],
    allow_bypass=False,
    plan_exit_mode="default",
)


class Echo(BaseModel):
    text: str


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": "echo", "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def looping(
    model_calls: int,
    each: Callable[[], Awaitable[None]] | None = None,
    said: str = "hello",
) -> Agent[None, str]:
    """model_calls model calls in one turn: model_calls - 1 echo calls, then an answer. `each`
    runs inside every echo call (between two model calls)."""

    async def run(args: Echo, _ctx: RunContext[None]) -> str:
        if each is not None:
            await each()
        return args.text

    echo = tool(
        name="echo",
        description="Echo text.",
        input=Echo,
        runs="host",
        execute=run,
        effect="read_only",
    )
    replies = [use({"text": said}, f"call_{i}") for i in range(1, model_calls)]
    script: JsonValue = {"responses": [*replies, text("Done.")]}
    return agent(model=scripted_model(script), tools=[echo], permissions=ALLOW)


async def query(store: Store, sql: str, *args: str | int) -> list[tuple[object, ...]]:
    """Rows of a test query on the store's own connection."""

    def run(conn: Conn) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = conn.execute(sql, args).fetchall()
        return rows

    return await (await open_store(store)).run(run)


async def cursors(store: Store, observer: str = "otel") -> dict[str, int]:
    """The observer's cursor per branch."""
    rows = await query(
        store, "SELECT branch_id, seq FROM observer_cursors WHERE observer = ?", observer
    )
    return {str(b): int(str(s)) for b, s in rows}


async def losses(store: Store) -> list[tuple[str, int, bool]]:
    """(thread_id, unchecked_events, reported) of every loss row."""
    rows = await query(
        store,
        "SELECT thread_id, unchecked_events, reported_at IS NOT NULL FROM observer_losses"
        " ORDER BY thread_id",
    )
    return [(str(t), int(str(n)), bool(r)) for t, n, r in rows]


def truncated(export: bytes, branch: BranchId, seq: int) -> bytes:
    """A branch export cut after its line `seq`, closed by a matching head: a crash there."""
    kept = [ln for ln in export.split(b"\n") if ln and b'"threads.head"' not in ln]
    kept = [ln for ln in kept if int(json.loads(ln).get("seq", 0)) <= seq]
    return b"\n".join(kept) + b"\n" + head_line(branch, seq, sha256_hex(kept[-1])) + b"\n"


async def imported(
    store: Store,
    case: str,
    through: dict[str, int] | None = None,
    only: tuple[str, ...] | None = None,
) -> None:
    """Imports a golden's branches (parents first, or `only` these) with the request artifacts
    they name."""
    sq = await open_store(store)
    d = CASES / case
    for path in sorted((d / "artifacts").glob("*")):
        await sq.put_artifact(path.read_bytes())
    names: list[str] = json.loads((d / "case.json").read_text())["branches"]
    for name in names if only is None else only:
        export = (d / f"{name}.jsonl").read_bytes()
        if through is not None and name in through:
            export = truncated(export, BranchId(name), through[name])
        log = verify_export(export, NOW)
        assert isinstance(log, Ok), log
        assert await sq.import_log(log.value) == Ok(None)

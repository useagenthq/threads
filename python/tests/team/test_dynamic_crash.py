"""A crash drill for a dynamic member (spec/schema/README.md, Teams, "Dynamic members"): the
process dies at every materialize of the member, after the lead's start recorded its define. The
restart's template no longer has the chosen tool, an MCP tool (so the lead's own listing, and its
pin, are unchanged): the member is rebound from the recorded choice, never re-resolved, so it ends
pin_unavailable once, with no model request. Mirrors TypeScript's
test/team/dynamic-crash.test.ts."""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue
from team.run_kit import call, say
from team.team_kit import assert_team_replays

from threads import (
    Agent,
    Completed,
    DynamicAgent,
    Principal,
    RunContext,
    agent,
    dynamic_agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.bindings import AppTool, Fence
from threads.agents.run import RunOptions, execute
from threads.agents.store import open_store
from threads.log import BranchId, MemberEndedEvent, MemberStartedEvent, ThreadId
from threads.loop.scripted import ScriptedModel
from threads.result import Ok
from threads.store.sql import text_of
from threads.team.rows import member_rows
from threads.thread.handle import Thread

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")


class CrashError(Exception):
    """The process dying at the member's materialize."""


class _Crashing(sqlite3.Connection):
    """A connection that dies at every materialize: the process never gets past it."""

    def execute(self, sql: str, parameters: Sequence[object] = (), /) -> sqlite3.Cursor:  # type: ignore[override] - narrowed for the drill
        if "UPDATE team_members SET branch_id" in sql:
            raise CrashError(sql)
        return super().execute(sql, parameters)


class _Doc(BaseModel):
    id: str


async def _text(_args: _Doc, _ctx: RunContext[object]) -> str:
    return "text"


class _Docs:
    """An MCP server double that lists one tool, mcp__docs__<name>."""

    def __init__(self, name: str) -> None:
        self._tool = name

    @property
    def name(self) -> str:
        return "docs"

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]:
        return self._session()

    @asynccontextmanager
    async def _session(self) -> AsyncGenerator[Sequence[AppTool[object]]]:
        yield (
            tool(
                name=f"mcp__docs__{self._tool}",
                description="Read a document.",
                input=_Doc,
                runs="host",
                effect="read_only",
                execute=_text,
            ),
        )


def _template(name: str, model: ScriptedModel | None = None) -> DynamicAgent[None, object]:
    fast = model or scripted_model({"responses": []})
    return dynamic_agent(name="specialist", models={"fast": fast}, tools=[_Docs(name)])


def _lead(template: DynamicAgent[None, object], *, fresh: bool) -> Agent[None, str]:
    args: JsonValue = {
        "agent": "specialist",
        "task": "Is INV-1002 paid?",
        "tools": ["mcp__docs__read_a"],
    }
    script = (
        [call("c1", "start", args), say("Started.")]
        if fresh
        else [say("The specialist could not run.")]
    )
    return agent(name="lead", model=scripted_model({"responses": script}), team=[template])


def test_a_crash_at_a_dynamic_members_materialize_then_a_template_without_its_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        crashing = sqlite(str(tmp_path))
        with monkeypatch.context() as m:
            m.setattr(sqlite3, "connect", partial(sqlite3.connect, factory=_Crashing))
            await open_store(crashing)
        with pytest.raises(CrashError):
            await _lead(_template("read_a"), fresh=True).run("Is INV-1002 paid?", store=crashing)

        # The restart's template lists read_b; the lead chose read_a.
        store = sqlite(str(tmp_path))
        sq = await open_store(store)
        teams = await sq.run(
            lambda c: c.execute("SELECT lead_thread_id, team_id FROM teams").fetchall()
        )
        ((lead_column, team_column),) = teams
        lead_thread, team = text_of(lead_column), text_of(team_column)
        lead_branch = await sq.root(ThreadId(lead_thread))
        assert isinstance(lead_branch, Ok)
        thread = Thread(ThreadId(lead_thread), lead_branch.value, store)
        member_model = scripted_model({"responses": [say("never")]})
        restarted = _lead(_template("read_b", member_model), fresh=False)
        options: RunOptions[None] = {"store": store, "principal": OPERATOR, "thread": thread}
        result = await execute(restarted.definition, None, options, None, lambda _e: None)
        assert isinstance(result, Completed), result
        assert result.output == "The specialist could not run."

        read = await sq.read(lead_branch.value, 0)
        assert isinstance(read, Ok)
        started = [e for e in read.value.fold.events if isinstance(e, MemberStartedEvent)]
        assert len(started) == 1
        rows = [r for r in await sq.run(lambda c: member_rows(c, team)) if r.role == "member"]
        assert [r.name for r in rows] == ["specialist-1"]
        assert rows[0].branch_id is not None
        member = await sq.read(BranchId(rows[0].branch_id), 0)
        assert isinstance(member, Ok)
        events = member.value.fold.events
        assert [e.type for e in events] == [
            "thread_started",
            "user_input",
            "turn_completed",
            "member_ended",
            "message_sent",
        ]
        ended = next(e for e in events if isinstance(e, MemberEndedEvent))
        outcome = ended.data.model_dump(mode="json", by_alias=True)["result"]
        assert outcome["status"] == "failed"
        assert outcome["error"]["code"] == "pin_unavailable"
        assert member_model.remaining == 1
        await assert_team_replays(sq, team)

    asyncio.run(main())

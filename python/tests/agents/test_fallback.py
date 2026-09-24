"""Model fallback through agent().run (ADR 0020): repeated overloads switch to the next fallback,
whose adapter and context window then serve the thread; with fallback_scope turn the next input
reverts to the primary, once per input, gated by before_model_switch, across a crash too."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, override

import pytest
from pydantic import JsonValue

from threads import (
    Agent,
    Completed,
    ConfigError,
    RunContext,
    Thread,
    agent,
    extension,
    scripted_model,
    sqlite,
)
from threads.agents.results import StreamEvent
from threads.agents.run import execute
from threads.agents.store import now_ms, open_store
from threads.hooks.types import SwitchGate
from threads.log import (
    Budget,
    Event,
    HookDecisionEvent,
    ModelRef,
    ModelRequestEvent,
    ModelSettings,
    Retry,
    SettingsChangedEvent,
    UserInputEvent,
)
from threads.loop.defaults import CONTEXT, effective_window
from threads.loop.drafts import draft
from threads.loop.model import ModelChunk, ModelContext, ModelInfo, ModelRequest
from threads.loop.scripted import SCRIPTED_INFO, ScriptedModel
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
OVERLOADED: JsonValue = {"error": {"reason": "overloaded", "http_status": 529}}
SMALL_WINDOW = 50_000
USER: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "t", "subject": "u"},
}


def retry(scope: Literal["turn", "thread"]) -> Retry:
    return Retry(
        max_retries=8,
        base_delay_ms=1,
        max_delay_ms=1,
        max_retry_after_ms=60_000,
        max_total_wait_ms=600_000,
        crash_resends=2,
        fallback_after=3,
        fallback_scope=scope,
        heartbeat_ms=15_000,
    )


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


class Renamed(ScriptedModel):
    """A scripted model under another name and window, so policy.models lists two models. It
    plays `inner`'s script."""

    def __init__(self, inner: ScriptedModel, info: ModelInfo) -> None:
        super().__init__([], {})
        self.inner = inner
        self._renamed = info

    @property
    @override
    def info(self) -> ModelInfo:
        return self._renamed

    @override
    def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        return self.inner.send(request, context)


def small(responses: Sequence[JsonValue], params: dict[str, JsonValue] | None = None) -> Renamed:
    limits = SCRIPTED_INFO.limits.model_copy(
        update={"name": "scripted-small", "context_window": SMALL_WINDOW}
    )
    ref = ModelRef(provider="scripted", name="scripted-small")
    info = replace(SCRIPTED_INFO, model=ref, limits=limits, params=params or SCRIPTED_INFO.params)
    return Renamed(scripted_model({"responses": list(responses)}), info)


@dataclass
class Switches:
    """A before_model_switch hook that answers `decision` and records what it was asked."""

    decision: Literal["allow", "deny"]
    asked: list[str] = field(default_factory=list[str])

    async def gate(self, settings: ModelSettings, _ctx: RunContext[None]) -> SwitchGate:
        self.asked.append(settings.model.name)
        if self.decision == "deny":
            return {"decision": "deny", "reason": "stay on this model"}
        return {"decision": "allow"}


def bot(
    primary: Sequence[JsonValue],
    fallback: Renamed,
    switches: Switches,
    scope: Literal["turn", "thread"] = "turn",
) -> Agent[None, str]:
    return agent(
        name="support",
        model=scripted_model({"responses": list(primary)}),
        fallback=[fallback],
        retry=retry(scope),
        extensions=[extension(name="ops", hooks={"before_model_switch": switches.gate})],
    )


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def only[T](events: Sequence[Event], kind: type[T]) -> list[T]:
    return [e for e in events if isinstance(e, kind)]


def after(events: Sequence[Event], input: UserInputEvent) -> list[Event]:
    return [e for e in events if e.seq > input.seq]


def keyed(events: Sequence[Event], input: UserInputEvent) -> list[HookDecisionEvent]:
    """The before_model_switch decisions on `input`: one at most, ever."""
    return [
        e
        for e in only(events, HookDecisionEvent)
        if e.data.hook == "before_model_switch" and e.data.input_event_id == input.event_id
    ]


def prefixes(events: Sequence[Event]) -> list[JsonValue]:
    return [e.data.declared_prefix.model_dump() for e in only(events, ModelRequestEvent)]


async def fell_back(
    fallback: Renamed, switches: Switches, scope: Literal["turn", "thread"] = "turn"
) -> Thread:
    """Turn 1: three overloaded answers, then the fallback answers."""
    result = await bot([OVERLOADED] * 3, fallback, switches, scope).run(
        "hi", store=sqlite(":memory:")
    )
    assert isinstance(result, Completed)
    assert result.output == "from the fallback"
    return result.thread


def test_repeated_overloads_switch_to_the_fallback_model_and_its_window() -> None:
    async def main() -> None:
        fallback = small([text("from the fallback")])
        thread = await fell_back(fallback, Switches("allow"))
        events = await events_of(thread)
        (changed,) = only(events, SettingsChangedEvent)
        assert (changed.data.reason, changed.data.settings.model.name) == (
            "fallback",
            "scripted-small",
        )
        # The request after the switch reached the fallback's own adapter.
        assert len(fallback.inner.sent) == 1
        sq = await open_store(thread.store)
        reader = await sq.acquire(thread.branch, "reader", now_ms)
        assert isinstance(reader, Ok)
        # Thresholds follow the current epoch's window: a smaller fallback compacts earlier.
        assert effective_window(reader.value.fold) == SMALL_WINDOW - CONTEXT.reserve_tokens

    asyncio.run(main())


def test_the_next_input_reverts_to_the_primary_before_its_first_request() -> None:
    async def main() -> None:
        switches = Switches("allow")
        thread = await fell_back(small([text("from the fallback")]), switches)
        again = bot([text("from the primary")], small([]), switches)
        result = await again.run("again", thread=thread)
        assert isinstance(result, Completed)
        assert result.output == "from the primary"
        events = await events_of(thread)
        u = only(events, UserInputEvent)[-1]
        turn = after(events, u)
        assert [e.type for e in turn][:3] == ["hook_decision", "settings_changed", "model_request"]
        (revert,) = only(turn, SettingsChangedEvent)
        assert revert.data.reason == "revert"
        assert (revert.data.cause_event_id, revert.data.settings.model.name) == (
            u.event_id,
            "scripted-1",
        )
        assert [d.data.decision for d in keyed(events, u)] == ["allow"]
        # The turn's request declares the primary's line 0, exactly as turn 1 first did.
        assert prefixes(turn)[0] == prefixes(events)[0]
        assert switches.asked == ["scripted-small", "scripted-1"]

    asyncio.run(main())


def test_a_denied_revert_keeps_the_fallback_for_that_turn() -> None:
    async def main() -> None:
        fallback = small([text("from the fallback"), text("still the fallback")])
        switches = Switches("allow")
        thread = await fell_back(fallback, switches)
        switches.decision = "deny"
        result = await bot([], fallback, switches).run("again", thread=thread)
        assert isinstance(result, Completed)
        assert result.output == "still the fallback"
        events = await events_of(thread)
        u = only(events, UserInputEvent)[-1]
        assert [d.data.decision for d in keyed(events, u)] == ["deny"]
        assert only(after(events, u), SettingsChangedEvent) == []
        assert fallback.inner.remaining == 0

    asyncio.run(main())


def test_fallback_scope_thread_never_reverts() -> None:
    async def main() -> None:
        fallback = small([text("from the fallback"), text("still the fallback")])
        switches = Switches("allow")
        thread = await fell_back(fallback, switches, "thread")
        result = await bot([], fallback, switches, "thread").run("again", thread=thread)
        assert isinstance(result, Completed)
        assert result.output == "still the fallback"
        events = await events_of(thread)
        assert [s.data.reason for s in only(events, SettingsChangedEvent)] == ["fallback"]
        assert switches.asked == ["scripted-small"]

    asyncio.run(main())


async def crash_after_input(thread: Thread, *, denied: bool) -> UserInputEvent:
    """The log a killed run leaves: a new input is durable, and (when `denied`) the
    before_model_switch deny keyed to it, then nothing. The dead run's lease is gone by the time
    the thread is reopened."""
    sq = await open_store(thread.store)
    killed = await sq.acquire(thread.branch, "killed", now_ms)
    assert isinstance(killed, Ok)
    writer = killed.value
    user = replace(draft("user_input", {"source": "api", "text": "again"}), actor=USER)
    done = await writer.append([user], None)
    assert isinstance(done, Ok)
    (u,) = done.value
    assert isinstance(u, UserInputEvent)
    if denied:
        deny: dict[str, JsonValue] = {
            "extension": "ops",
            "hook": "before_model_switch",
            "decision": "deny",
            "reason": "stay on this model",
            "input_event_id": u.event_id,
        }
        assert isinstance(await writer.append([draft("hook_decision", deny)], None), Ok)
    await writer.release()
    return u


async def reopen(agent_: Agent[None, str], thread: Thread) -> None:
    """Continues the thread with no new input, as a host resumes it after a restart."""

    def drop(_item: StreamEvent) -> None:
        pass

    result = await execute(agent_.definition, None, {"thread": thread}, None, drop)
    assert isinstance(result, Completed)


def test_killed_before_the_revert_batch_reopen_reverts_exactly_once() -> None:
    async def main() -> None:
        switches = Switches("allow")
        thread = await fell_back(small([text("from the fallback")]), switches)
        u = await crash_after_input(thread, denied=False)
        await reopen(bot([text("from the primary")], small([]), switches), thread)
        events = await events_of(thread)
        reverts = [s for s in only(events, SettingsChangedEvent) if s.data.reason == "revert"]
        assert [r.data.cause_event_id for r in reverts] == [u.event_id]
        assert len(keyed(events, u)) == 1
        assert prefixes(after(events, u))[0] == prefixes(events)[0]
        assert switches.asked == ["scripted-small", "scripted-1"]

    asyncio.run(main())


def test_killed_after_a_recorded_deny_reopen_never_asks_again() -> None:
    async def main() -> None:
        fallback = small([text("from the fallback"), text("still the fallback")])
        switches = Switches("allow")
        thread = await fell_back(fallback, switches)
        before = prefixes(await events_of(thread))
        u = await crash_after_input(thread, denied=True)
        await reopen(bot([], fallback, switches), thread)
        events = await events_of(thread)
        assert switches.asked == ["scripted-small"]
        assert [d.data.decision for d in keyed(events, u)] == ["deny"]
        assert only(after(events, u), SettingsChangedEvent) == []
        # Sent on the fallback epoch: its line 0 is the fallback request's of turn 1.
        assert prefixes(after(events, u)) == [before[-1]]
        assert fallback.inner.remaining == 0

    asyncio.run(main())


def test_a_fallback_without_a_bound_under_a_budget_is_refused() -> None:
    unbounded = small([], {"temperature": 0})
    with pytest.raises(ConfigError) as raised:
        agent(
            model=scripted_model({"responses": []}),
            fallback=[unbounded],
            budget=Budget(max_output_tokens=10_000),
        )
    assert raised.value.code == "budget_unenforceable"
    assert "scripted-small" in str(raised.value)


PLAIN_HASH = "118df2e5df0d26f55795c55185e95d4b82bf0060d21b6c3653abee6bc35f1e8f"
"""The config_hash this agent pins: unused `output` and `fallback` add nothing to it. (It changed
once, when Python pins began recording the resolved default settings, as TypeScript's do.)"""


def test_a_thread_pinned_before_output_and_fallback_continues_and_fails_closed_on_a_change() -> (
    None
):
    def plain(fallback: Sequence[Renamed] = ()) -> Agent[None, str]:
        model = scripted_model({"responses": [text("one"), text("two")]})
        return agent(name="support", instructions="Help the user.", model=model, fallback=fallback)

    async def main() -> None:
        unchanged = plain()
        assert unchanged.definition.pin()[0]["config_hash"] == PLAIN_HASH
        first = await unchanged.run("hi", store=sqlite(":memory:"))
        again = await unchanged.run("more", thread=first.thread)
        assert isinstance(again, Completed)
        # Adding a fallback is a config change: no migration, the pin refuses it.
        with pytest.raises(ConfigError) as raised:
            await plain(fallback=[small([])]).run("more", thread=first.thread)
        assert raised.value.code == "invalid_config"

    asyncio.run(main())

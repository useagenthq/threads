"""agent(output_styles=...) and Thread.set_output_style (spec/api.json): the pinned text as a
trusted instruction after the prompt prefix, so line 0 and the history cache are kept; set only
by an operator between turns (semantic rule 29), and restored after a compaction."""

import asyncio

import pytest
from compact_kit import SUMMARY, body, logged, parked, requests, text
from pydantic import JsonValue

from threads import Completed, ConfigError, agent, scripted_model, sqlite
from threads.agents.store import now_ms, open_store
from threads.log import (
    BranchId,
    CompactedEvent,
    InjectedEvent,
    ParseError,
)
from threads.result import Err, Ok
from threads.store import Draft, ForkRequest
from threads.store.lines import uuid7
from threads.thread.control import LOCAL_OPERATOR
from threads.thread.handle import Thread

OP = LOCAL_OPERATOR
STYLES = {"concise": "Answer in at most three sentences.", "plain": "Reply as usual."}


def test_styles_are_pinned_and_an_agent_without_them_pins_nothing() -> None:
    model = scripted_model({"responses": []})
    styled = agent(model=model, output_styles=STYLES).definition.thread_started()
    plain = agent(model=model).definition.thread_started()
    policy = styled["policy"]
    assert isinstance(policy, dict)
    assert policy["output_styles"] == STYLES
    plain_policy = plain["policy"]
    assert isinstance(plain_policy, dict)
    assert "output_styles" not in plain_policy
    assert agent(model=model, output_styles={}).definition.thread_started() == plain
    assert styled["config_hash"] != plain["config_hash"]


@pytest.mark.parametrize(
    "styles",
    [{"": "Short."}, {"short": ""}, {"short": 3}],
    ids=["empty name", "empty text", "not text"],
)
def test_an_invalid_style_is_a_config_error_naming_it(styles: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match=r"output_styles\[") as raised:
        agent(model=scripted_model({"responses": []}), output_styles=styles)  # type: ignore[arg-type] - a wrong value on purpose
    assert raised.value.code == "invalid_config"


def test_a_style_is_an_instruction_after_the_prefix_and_keeps_the_cache() -> None:
    script: JsonValue = {"responses": [text("Hi."), text("Short.")]}

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model(script), output_styles=STYLES)
        first = await bot.run("hi", store=store)
        styled = await first.thread.set_output_style("concise", OP)
        assert isinstance(styled, Ok)
        second = await bot.run("again", store=store, thread=first.thread)
        assert isinstance(second, Completed)
        events = await logged(second.thread)
        style = next(
            e for e in events if isinstance(e, InjectedEvent) and e.data.source == "output_style"
        )
        assert (style.event_id, style.actor.kind) == (styled.value.event_id, "user")
        before, after = requests(events, side=False)
        old, new = await body(second.thread, before), await body(second.thread, after)
        assert old.split("\n")[0] == new.split("\n")[0]
        assert before.data.declared_prefix == after.data.declared_prefix
        assert new.startswith(old[: old.rindex("\n", 0, len(old) - 1) + 1])
        assert '<context source=\\"output_style\\" id=\\"concise\\">' in new

    asyncio.run(main())


def test_an_unknown_style_names_the_defined_ones() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        styled = agent(model=scripted_model({"responses": [text("Hi.")]}), output_styles=STYLES)
        first = await styled.run("hi", store=store)
        missing = await first.thread.set_output_style("loud", OP)
        assert missing == Err(
            ParseError("not_found", "no output style loud; the agent defines concise, plain")
        )
        plain = agent(model=scripted_model({"responses": [text("Hi.")]}))
        other = await plain.run("hi", store=sqlite(":memory:"))
        none = await other.thread.set_output_style("concise", OP)
        assert none == Err(
            ParseError("not_found", "no output style concise; the agent defines none")
        )

    asyncio.run(main())


def test_a_style_waits_for_a_parked_turn() -> None:
    async def main() -> None:
        thread = await parked()
        busy = await thread.set_output_style("concise", OP)
        assert isinstance(busy, Err)
        assert busy.error.code == "branch_busy"

    asyncio.run(main())


def test_an_inspection_only_branch_is_not_runnable() -> None:
    from thread.test_handle import opened, snapshot, world  # noqa: PLC0415 - fork fixtures

    async def main() -> None:
        w = await world()
        try:
            snap = await snapshot(w)
            repair = BranchId(uuid7(now_ms()))
            request = ForkRequest(w.writer.branch_id, snap.seq, repair, {"reason": "repair"})
            assert await w.sq.fork(request, "operator", now_ms) == Ok(None)
            handle = await opened(w, branch_id=repair)
            for refused in (
                await handle.set_output_style("concise", OP),
                await handle.compact(OP),
            ):
                assert isinstance(refused, Err)
                assert refused.error.code == "branch_not_runnable"
        finally:
            await w.sq.close()

    asyncio.run(main())


async def _append(thread: Thread, drafts: list[Draft]) -> Ok[object] | Err[ParseError]:
    sq = await open_store(thread.store)
    writer = await sq.acquire(thread.branch, "test", now_ms)
    assert isinstance(writer, Ok)
    try:
        done = await writer.value.append(drafts)
        return Ok(None) if isinstance(done, Ok) else done
    finally:
        await writer.value.release()


@pytest.mark.parametrize(
    ("change", "actor"),
    [
        ({"text": "Shout."}, "user"),
        ({"origin": {"id": "loud"}}, "user"),
        ({}, "model"),
        ({}, "host"),
        (
            {"text": None, "ref": {"sha256": "0" * 64, "bytes": 1, "media_type": "text/plain"}},
            "user",
        ),
    ],
    ids=["other text", "unknown name", "by the model", "a host style with no compaction", "a ref"],
)
def test_rule_29_refuses_a_style_the_pin_doesnt_hold(
    change: dict[str, JsonValue], actor: str
) -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": [text("Hi.")]}), output_styles=STYLES)
        done = await bot.run("hi", store=sqlite(":memory:"))
        data: dict[str, JsonValue] = {
            "source": "output_style",
            "trust": "trusted_instruction",
            "origin": {"id": "concise"},
            "text": STYLES["concise"],
            **change,
        }
        if data.get("text") is None:
            del data["text"]
        who: dict[str, JsonValue] = {"kind": actor}
        if actor == "user":
            who["principal"] = {"issuer": "api", "tenant": "local", "subject": "operator"}
        refused = await _append(done.thread, [Draft("injected", data, who)])
        assert isinstance(refused, Err)
        assert refused.error.code == "invalid_transition"

    asyncio.run(main())


def test_a_compaction_restores_the_style_it_drops() -> None:
    script: JsonValue = {"responses": [text("Hi."), text("One."), text(SUMMARY), text("Two.")]}

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model(script), output_styles=STYLES)
        first = await bot.run("hi", store=store)
        assert isinstance(await first.thread.set_output_style("concise", OP), Ok)
        await bot.run("one", store=store, thread=first.thread)
        assert isinstance(await first.thread.compact(OP), Ok)
        await bot.run("two", store=store, thread=first.thread)
        events = await logged(first.thread)
        at = next(i for i, e in enumerate(events) if isinstance(e, CompactedEvent))
        restored = events[at + 1]
        assert isinstance(restored, InjectedEvent)
        assert (restored.data.source, restored.actor.kind) == ("output_style", "host")
        assert events[at].seq + 1 == restored.seq
        last = await body(first.thread, requests(events, side=False)[-1])
        assert "output_style" in last

    asyncio.run(main())


def test_adding_a_style_to_the_agent_is_a_new_config() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        plain = agent(model=scripted_model({"responses": [text("Hi.")]}))
        first = await plain.run("hi", store=store)
        styled = agent(model=scripted_model({"responses": [text("Hi.")]}), output_styles=STYLES)
        with pytest.raises(ConfigError, match="another config"):
            await styled.run("again", store=store, thread=first.thread)
        missing = await first.thread.set_output_style("concise", OP)
        assert isinstance(missing, Err)
        assert missing.error.code == "not_found"

    asyncio.run(main())

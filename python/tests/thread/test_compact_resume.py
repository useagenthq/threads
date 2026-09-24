"""A requested compaction resumed after a crash at each boundary: every step is decided from the
log, so a resumed run never re-runs a recorded hook, never re-sends a recorded summary, and
re-sends a crashed attempt only within crash_resends (spec/schema/README.md, "Requested
compaction and output styles")."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

import pytest
from compact_kit import SUMMARY, body, logged, request_of, requests, text

from threads import Agent, agent, scripted_model, sqlite
from threads.agents.context import RunContext
from threads.agents.run import execute
from threads.agents.store import now_ms, open_store
from threads.hooks.extension import extension
from threads.hooks.types import CompactGate
from threads.log import (
    CompactedEvent,
    CompactionFailedEvent,
    CompactionRequestedEvent,
    ContextEditedEvent,
    EventId,
)
from threads.log.digest import sha256_hex
from threads.loop.drafts import draft
from threads.loop.scripted import ScriptedModel
from threads.reduce.state import ReducedState
from threads.result import Ok
from threads.store import Draft, SqliteStore, Writer
from threads.thread.control import LOCAL_OPERATOR
from threads.thread.handle import Thread

if TYPE_CHECKING:
    from pydantic import JsonValue

USER: "dict[str, JsonValue]" = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "local", "subject": "operator"},
}


def _drop(_item: object) -> None:
    pass


async def _side(
    sq: SqliteStore, writer: Writer, request: CompactionRequestedEvent, attempt: int
) -> EventId:
    """A side request naming the request, with the bytes the loop would have sent."""
    got = await sq.render(writer.fold.events, compaction=True, cause=request.event_id)
    assert isinstance(got, Ok)
    rendered = got.value
    sha = await sq.put_artifact(rendered.body)
    data: dict[str, JsonValue] = {
        "attempt": attempt,
        "purpose": "compaction",
        "cause_event_id": request.event_id,
        "declared_prefix": {"bytes": len(rendered.line0), "sha256": sha256_hex(rendered.line0)},
        "request_ref": {
            "sha256": sha,
            "bytes": len(rendered.body),
            "media_type": "application/x-ndjson",
        },
    }
    done = await writer.append([draft("model_request", data)])
    assert isinstance(done, Ok)
    return done.value[0].event_id


async def _crashed(
    bot: Agent[None, str], steps: Sequence[str], hook: Sequence[Draft] = ()
) -> Thread:
    """hi, then compact, then the next input and `steps` (crash, prompt_too_long,
    server_error, response) as a crashed run left them."""
    store = sqlite(":memory:")
    first = await bot.run("hi", store=store, deps=None)
    assert isinstance(await first.thread.compact(LOCAL_OPERATOR), Ok)
    sq = await open_store(store)
    got = await sq.acquire(first.thread.branch, "crashed", now_ms)
    assert isinstance(got, Ok)
    writer = got.value
    request = request_of(writer.fold.events)
    user_input = Draft("user_input", {"source": "api", "text": "go on"}, USER)
    assert isinstance(await writer.append([user_input, *hook]), Ok)
    for n, step in enumerate(steps, start=1):
        side = await _side(sq, writer, request, n)
        if step == "response":
            data: dict[str, JsonValue] = {
                "request_event_id": side,
                "content": [{"type": "text", "text": SUMMARY}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "completeness": "complete",
            }
            assert isinstance(await writer.append([draft("model_response", data, "model")]), Ok)
            continue
        outcome = "unknown" if step == "crash" else "failed"
        actor = "recovery" if step == "crash" else "host"
        abandoned: dict[str, JsonValue] = {
            "request_event_id": side,
            "provider_outcome": outcome,
            "reason": step,
        }
        assert isinstance(
            await writer.append([draft("model_attempt_abandoned", abandoned, actor)]), Ok
        )
    await writer.release()
    return first.thread


async def _resume(bot: Agent[None, str], thread: Thread) -> None:
    await execute(bot.definition, None, {"thread": thread}, None, _drop)


def _sides_sent(model: ScriptedModel) -> int:
    return sum(b"Reply with the summary only." in r.body for r in model.sent)


@pytest.mark.parametrize(("crashes", "resent"), [(1, 1), (2, 1), (3, 0)])
def test_a_recorded_crash_is_resent_only_within_the_budget(crashes: int, resent: int) -> None:
    async def main() -> None:
        model = scripted_model({"responses": [text("Hi."), text(SUMMARY), text("Answer.")]})
        bot = agent(model=model)
        thread = await _crashed(bot, ["crash"] * crashes)
        sent_before = len(model.sent)
        for _ in range(2):  # resumed twice: one decision, never an extra send
            await _resume(bot, thread)
        assert _sides_sent(model) == resent
        assert len(model.sent) - sent_before == resent + 1  # and the turn's own request
        events = await logged(thread)
        if resent:
            assert any(isinstance(e, CompactedEvent) for e in events)
        else:
            failed = [e for e in events if isinstance(e, CompactionFailedEvent)]
            assert [f.data.reason for f in failed] == ["model_error"]

    asyncio.run(main())


def test_a_recorded_summary_is_never_sent_again() -> None:
    async def main() -> None:
        model = scripted_model({"responses": [text("Hi."), text("Answer.")]})
        bot = agent(model=model)
        thread = await _crashed(bot, ["response"])
        await _resume(bot, thread)
        await _resume(bot, thread)
        assert _sides_sent(model) == 0
        events = await logged(thread)
        compacted = next(e for e in events if isinstance(e, CompactedEvent))
        assert (compacted.data.trigger, compacted.actor.kind) == ("manual", "recovery")
        assert model.remaining == 0

    asyncio.run(main())


@pytest.mark.parametrize(
    ("steps", "outcome"),
    [
        (["server_error"], "model_error"),
        (["rate_limited"], "model_error"),
        (["prompt_too_long", "prompt_too_long"], "prompt_too_long"),
    ],
)
def test_a_recorded_refusal_is_answered_with_no_new_request(steps: list[str], outcome: str) -> None:
    async def main() -> None:
        model = scripted_model({"responses": [text("Hi."), text("Answer.")]})
        bot = agent(model=model)
        thread = await _crashed(bot, steps)
        await _resume(bot, thread)
        assert _sides_sent(model) == 0
        failed = [e for e in await logged(thread) if isinstance(e, CompactionFailedEvent)]
        assert [f.data.reason for f in failed] == [outcome]

    asyncio.run(main())


@pytest.mark.parametrize("steps", [["prompt_too_long"], ["crash", "prompt_too_long"]])
def test_the_one_fallback_runs_once_even_after_a_crash(steps: list[str]) -> None:
    async def main() -> None:
        model = scripted_model({"responses": [text("Hi."), text(SUMMARY), text("Answer.")]})
        bot = agent(model=model)
        thread = await _crashed(bot, steps)
        await _resume(bot, thread)
        assert _sides_sent(model) == 1
        events = await logged(thread)
        attempts = [r.data.attempt for r in requests(events, side=True)]
        assert attempts == [*range(1, len(steps) + 2)]
        assert any(isinstance(e, CompactedEvent) for e in events)
        edits = [e for e in events if isinstance(e, ContextEditedEvent)]
        assert all(e.data.reason == "compaction_fallback" for e in edits)

    asyncio.run(main())


def test_a_recorded_hook_decision_is_reused() -> None:
    called: list[str] = []

    def guide(name: str, words: str) -> Callable[..., Awaitable[CompactGate]]:
        async def hook(_state: ReducedState, _ctx: RunContext[None]) -> CompactGate:
            called.append(name)
            return {"decision": "guide", "text": words}

        return hook

    async def main() -> None:
        first = extension(name="first", hooks={"before_compact": guide("first", "Keep A.")})
        second = extension(name="second", hooks={"before_compact": guide("second", "Keep B.")})
        model = scripted_model({"responses": [text("Hi."), text(SUMMARY), text("Answer.")]})
        bot = agent(model=model, extensions=[first, second])
        recorded = draft(
            "hook_decision",
            {
                "extension": "first",
                "hook": "before_compact",
                "decision": "guide",
                "reason": "Keep A.",
            },
        )
        thread = await _crashed(bot, [], hook=[recorded])
        await _resume(bot, thread)
        assert called == ["second"]
        (side,) = requests(await logged(thread), side=True)
        sent = await body(thread, side)
        assert sent.count("Keep A.") == 1
        assert sent.count("Keep B.") == 1
        assert "Additional instructions:\\nKeep A.\\nKeep B." in sent

    asyncio.run(main())


def test_a_crashed_side_request_with_a_cancel_pending_is_answered_failed_not_resent() -> None:
    """Recovery answers the request (compaction_failed) rather than leaving it to run in a later
    turn: nothing is sent again after the cancel."""
    model = scripted_model({"responses": [text("Hi."), text(SUMMARY), text("A.")]})
    bot = agent(model=model)

    async def main() -> list[str]:
        cancel = Draft("cancel_requested", {"scope": "thread"}, USER)
        thread = await _crashed(bot, ["crash"], [cancel])
        await _resume(bot, thread)
        events = await logged(thread)
        assert len(requests(events, side=True)) == 1
        failed = [e for e in events if isinstance(e, CompactionFailedEvent)]
        assert len(failed) == 1
        return [e.type for e in events]

    names = asyncio.run(main())
    assert names[-2:] == ["cancelled", "turn_completed"]

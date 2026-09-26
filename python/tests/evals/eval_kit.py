"""Shared pieces of the eval tests: a support agent with a read-only lookup and an effectful refund,
scripted replies, and a saved case (the Python twin of typescript test/evals/kit.ts)."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, JsonValue

from threads import Agent, CaseExpectation, Completed, RunContext, agent, scripted_model, sqlite
from threads import tool as make_tool
from threads.hooks.extension import Extension
from threads.log import Model as ModelLimits
from threads.log import ModelRef, Permissions
from threads.loop.model import ModelInfo
from threads.loop.scripted import SCRIPTED_INFO, ScriptedModel
from threads.result import Ok
from threads.thread.case import SavedCase
from threads.thread.case_simulate import Simulate

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, input: dict[str, JsonValue], call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": input}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Order(BaseModel):
    id: str


class Refunds:
    """How many refunds the tool body really issued: an eval must never add one."""

    count = 0


async def _lookup(args: Order, _ctx: RunContext[None]) -> str:
    return f"order {args.id}: shipped 12 days ago"


async def _refund(args: Order, _ctx: RunContext[None]) -> str:
    Refunds.count += 1
    return f"refunded order {args.id}"


LOOKUP = make_tool(
    name="lookup_order",
    description="Look up an order by id.",
    input=Order,
    runs="host",
    effect="read_only",
    execute=_lookup,
)
REFUND = make_tool(
    name="issue_refund",
    description="Refund an order.",
    input=Order,
    runs="host",
    effect="unguarded",
    execute=_refund,
)

REFUND_TURN: tuple[JsonValue, ...] = (
    use("lookup_order", {"id": "42"}, "c1"),
    use("issue_refund", {"id": "42"}, "c2"),
    say("Refunded order 42; it is inside the 30-day window."),
)

ALLOW = Permissions(
    mode="default",
    allow=["lookup_order", "issue_refund"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


def support(
    responses: Sequence[JsonValue],
    extensions: Sequence[Extension[None]] = (),
    instructions: str = "You handle refunds.",
) -> Agent[None, str]:
    return agent(
        name="support",
        instructions=instructions,
        model=scripted_model({"responses": list(responses)}),
        tools=[LOOKUP, REFUND],
        permissions=ALLOW,
        extensions=list(extensions),
    )


async def saved(
    folder: Path, name: str = "refund-policy", rubric: tuple[str, ...] | None = None
) -> SavedCase:
    """The refund turn run on a fresh thread and saved as `folder/name`."""
    bot = support(REFUND_TURN)
    run = await bot.run("Please refund order 42.", store=sqlite(":memory:"))
    assert isinstance(run, Completed), run
    got = await run.thread.save_case(
        name,
        expect=CaseExpectation(must=({"type": "tool_call", "data": {"name": "lookup_order"}},)),
        external_effects="stub",
        rubric=rubric,
        dir=str(folder),
    )
    assert isinstance(got, Ok), got
    return got.value


def user_replies(replies: Sequence[str | bool | dict[str, JsonValue]]) -> list[JsonValue]:
    """The simulated user's replies, in order: each is one structured UserTurn output. A string is
    a message it sends; True is the user stopping; a dict is raw output. Call ids are unique."""
    out: list[JsonValue] = []
    for i, reply in enumerate(replies):
        if isinstance(reply, str):
            output: dict[str, JsonValue] = {"message": reply, "done": False}
        elif isinstance(reply, bool):
            output = {"message": "", "done": True}
        else:
            output = reply
        out.append(use("final_output", output, f"u{i + 1}"))
    return out


async def saved_turns(  # noqa: PLR0913 - one saved case, and every option it takes
    folder: Path,
    name: str,
    target: Agent[None, str],
    inputs: Sequence[str],
    *,
    must: dict[str, JsonValue] | None = None,
    rubric: tuple[str, ...] | None = None,
    simulate: Simulate | None = None,
) -> SavedCase:
    """Runs `inputs` in order on one thread and saves the last turn as `folder/name`. Earlier
    inputs become the case's prefix, which a simulated live run continues or re-drives."""
    store = sqlite(":memory:")
    thread = None
    for text in inputs:
        run = (
            await target.run(text, store=store, thread=thread)
            if thread
            else await target.run(text, store=store)
        )
        assert isinstance(run, Completed), run
        thread = run.thread
    assert thread is not None, "saved_turns needs an input"
    got = await thread.save_case(
        name,
        expect=CaseExpectation(must=(must or {"type": "turn_completed"},)),
        external_effects="stub",
        rubric=rubric,
        simulate=simulate,
        dir=str(folder),
    )
    assert isinstance(got, Ok), got
    return got.value


PRICE: JsonValue = {"input": 1000, "output": 5000}


class Priced(ScriptedModel):
    """A scripted model under its own name, declaring a price: its runs have a cost, and the
    request guard still allows it."""

    def __init__(self, responses: Sequence[JsonValue], name: str) -> None:
        inner = scripted_model({"responses": list(responses)})
        super().__init__([], {})
        self._entries = inner._entries
        limits = {**SCRIPTED_INFO.limits.model_dump(mode="json"), "name": name, "price": PRICE}
        self._priced = replace(
            SCRIPTED_INFO,
            model=ModelRef(provider="scripted", name=name),
            limits=ModelLimits.model_validate(limits),
        )

    @property
    def info(self) -> ModelInfo:
        return self._priced


def priced(responses: Sequence[JsonValue], name: str = "scripted-priced") -> Priced:
    return Priced(responses, name)


def verdicts_reply(passes: Sequence[bool]) -> JsonValue:
    """The judge's structured answer: one verdict per criterion."""
    verdicts: list[JsonValue] = [
        {"criterion": i + 1, "pass": p, "reason": "It does." if p else "It does not."}
        for i, p in enumerate(passes)
    ]
    return use("final_output", {"verdicts": verdicts}, "v1")

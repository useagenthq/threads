"""The fixture agents the exposed side is driven with. Every one plays a scripted model: no test
here ever reaches a real provider, and the global model-request guard proves it.

Each carries a price, because the scripted model declares none and a cost budget that cannot be
enforced refuses every attempt. Without one, every test here would pass its default budget by being
rejected at the start, and the states it is meant to reach would never be reached at all. The Python
twin of typescript/packages/host/test/a2a/agents.ts."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Final

from pydantic import BaseModel, JsonValue

from threads import Agent, RunContext, agent, scripted_model, tool
from threads.agents.tool import Tool
from threads.log import Model as ModelLimits
from threads.log import ModelRef, Principal
from threads.loop.model import ModelInfo
from threads.loop.scripted import SCRIPTED_INFO, ScriptedModel

USAGE: Final[JsonValue] = {"input_tokens": 10, "output_tokens": 2}
PRICE: Final[JsonValue] = {"input": 1, "output": 1}
"""Nano-units per token: a fraction of a cent, so the default budget is far from binding."""


def say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: Mapping[str, JsonValue], call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": dict(args)}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Priced(ScriptedModel):
    """A scripted model that declares a price, so its runs have an enforceable cost."""

    def __init__(self, responses: Sequence[JsonValue]) -> None:
        inner = scripted_model({"responses": list(responses)})
        super().__init__([], {})
        self._entries = inner._entries
        name = "scripted-a2a"
        limits = {**SCRIPTED_INFO.limits.model_dump(mode="json"), "name": name, "price": PRICE}
        self._priced = replace(
            SCRIPTED_INFO,
            model=ModelRef(provider="scripted", name=name),
            limits=ModelLimits.model_validate(limits),
        )

    @property
    def info(self) -> ModelInfo:
        return self._priced


class Order(BaseModel):
    id: str


async def _look_up(args: Order, _ctx: RunContext[None]) -> str:
    return f"order {args.id}"


async def _refund(_args: Order, _ctx: RunContext[None]) -> str:
    return "refunded"


def _lookup_tool() -> Tool[Order, str, None]:
    return tool(
        name="lookup_order",
        description="Look an order up.",
        input=Order,
        runs="host",
        effect="read_only",
        execute=_look_up,
    )


def talker(*texts: str) -> Agent[None, str]:
    """No tools at all: every row that needs no approver uses this."""
    return agent(
        name="support", instructions="Never publish me.", model=Priced([say(t) for t in texts])
    )


def reader(*texts: str) -> Agent[None, str]:
    """Read-only tools only: the approvers rule must not ask for approvers here."""
    return agent(name="support", model=Priced([say(t) for t in texts]), tools=[_lookup_tool()])


def actor(responses: Sequence[JsonValue], approvers: Sequence[Principal]) -> Agent[None, str]:
    """A tool that is not read_only: an exposed agent needs approvers to have one."""
    refund = tool(
        name="refund", description="Refund an order.", input=Order, runs="host", execute=_refund
    )
    return agent(name="support", model=Priced(responses), tools=[refund], approvers=list(approvers))


def asker(question: str, answer: str, options: Sequence[str] | None = None) -> Agent[None, str]:
    """Asks one question, then answers with the text it was given."""
    args: dict[str, JsonValue] = {"question": question}
    if options is not None:
        args["options"] = list(options)
    return agent(name="support", model=Priced([use("ask_user", args, "q1"), say(answer)]))


class City(BaseModel):
    city: str


def structured(city: str) -> Agent[None, City]:
    """An output schema, so a completed task's artifact is a data part rather than text."""
    return agent(
        name="support", model=Priced([use("final_output", {"city": city}, "o1")]), output=City
    )


def worker() -> Agent[None, str]:
    """A run that needs two model requests: a read-only tool call, then a reply. With a budget of
    one request the second is refused, so the task ends after it has run rather than at the
    start."""
    return agent(
        name="support",
        model=Priced([use("lookup_order", {"id": "1"}, "c1"), say("done")]),
        tools=[_lookup_tool()],
    )


def handoff_pair() -> Agent[None, str]:
    """Two agents where the first may hand the conversation to the second."""
    other = agent(name="billing", model=Priced([say("billing here")]))
    return agent(name="support", model=Priced([say("hi")]), handoffs=[other])

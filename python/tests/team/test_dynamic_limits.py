"""A dynamic member's limits: a sandboxed template's member that didn't choose bash has no bash,
and start's headroom is for the chosen model. Mirrors TypeScript's test/team/dynamic.test.ts."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

from pydantic import JsonValue
from team.run_kit import call, events, member_events, say
from team.test_dynamic import INVOICE, preview, scripted, specialist_of, start_specialist

from threads import Completed, agent, dynamic_agent, fake_sandbox, sqlite, usd
from threads.log import Budget, ThreadStartedEvent, ToolResultEvent
from threads.log import Model as ModelLimits
from threads.loop.model import ModelInfo
from threads.loop.scripted import ScriptedModel


def test_a_sandboxed_templates_member_that_chose_one_tool_has_no_bash() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        fast = scripted([call("m1", "bash", {"command": "ls"}), say("No shell.")])
        template = dynamic_agent(
            name="specialist", tools=[INVOICE], models={"fast": fast}, sandbox=fake_sandbox()
        )
        script = [start_specialist("c1", tools=["invoice_status"]), say("Started."), say("Done.")]
        lead = agent(name="lead", model=scripted(script), team=[template])
        r = await lead.run("Work.", store=store)
        assert isinstance(r, Completed), r
        member = await member_events(store, r.team.ref.id, "specialist-1")
        pin = next(e for e in member if isinstance(e, ThreadStartedEvent))
        names = {t.name for t in pin.data.tools}
        assert names.isdisjoint({"bash", "read", "write", "edit"})
        result = next(e for e in member if isinstance(e, ToolResultEvent))
        assert result.data.preview == "unknown tool bash"

    asyncio.run(main())


def _priced(responses: Sequence[JsonValue], price: int) -> ScriptedModel:
    """A scripted model that declares a price per token (nano-dollars)."""
    model = _Priced(list(scripted(responses)._entries), {})  # pyright: ignore[reportPrivateUsage] - reuse the parsed script
    model.price = {"input": price, "output": price}
    return model


class _Priced(ScriptedModel):
    price: JsonValue = None

    @property
    def info(self) -> ModelInfo:
        base = super().info
        limits = {**base.limits.model_dump(mode="json"), "price": self.price}
        return replace(base, limits=ModelLimits.model_validate(limits))


def test_budget_headroom_is_for_the_chosen_model() -> None:
    """The strong model has no room under the run's cost budget; the fast one does."""

    async def main() -> None:
        store = sqlite(":memory:")
        fast = _priced([say("Paid.")], 1)
        strong = _priced([], 10**6)
        script = [
            start_specialist("c1", model="strong"),
            start_specialist("c2", model="fast"),
            say("Started."),
            say("Done."),
        ]
        lead = agent(name="lead", model=_priced(script, 1), team=[specialist_of(fast, strong)])
        r = await lead.run("Work.", store=store, budget=Budget(max_cost_nanos=usd(0.01)))
        log = await events(store, r.thread)
        assert preview(log, "c1") == {"code": "budget_exceeded", "status": "refused"}
        second = preview(log, "c2")
        assert isinstance(second, dict)
        assert second["status"] == "started"

    asyncio.run(main())

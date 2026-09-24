"""An agent with one echo tool and one hooked extension, run on a scripted model: what the hook
tests drive, and the helpers that read the log back."""

from collections.abc import Sequence

from pydantic import BaseModel, JsonValue

from threads import RunContext, agent, scripted_model, sqlite, tool
from threads.agents.results import RunResult
from threads.hooks.extension import extension
from threads.hooks.types import Hooks
from threads.log import Event, HookDecisionEvent, Permissions
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALLOW = Permissions(
    mode="default",
    allow=["echo"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": call_id,
        "name": "echo",
        "input": {"text": "hi"},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Echo(BaseModel):
    text: str


class Box:
    """The echo tool, counting how often its body ran."""

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, args: Echo, _ctx: RunContext[None]) -> str:
        self.runs += 1
        return f"echo {args.text} SECRET=hunter2"


class Accent(Box):
    """An echo whose result starts with a two-byte character."""

    async def run(self, args: Echo, _ctx: RunContext[None]) -> str:
        return "\u00e9" + await super().run(args, _ctx)


async def run(
    hooks: Hooks[None],
    responses: Sequence[JsonValue],
    box: Box | None = None,
    permissions: Permissions = ALLOW,
) -> tuple[RunResult[str], list[Event]]:
    box = box or Box()
    echo = tool(name="echo", description="Echo.", input=Echo, runs="host", execute=box.run)
    bot = agent(
        model=scripted_model({"responses": list(responses)}),
        tools=[echo],
        permissions=permissions,
        extensions=[extension(name="ops", hooks=hooks, hook_timeout_ms=50)],
    )
    result = await bot.run("go", store=sqlite(":memory:"), deps=None)
    timeline = await result.thread.timeline()
    assert isinstance(timeline, Ok)
    return result, [e.event for e in timeline.value.entries]


def decisions(events: Sequence[Event]) -> list[tuple[str, str]]:
    return [(e.data.hook, e.data.decision) for e in events if isinstance(e, HookDecisionEvent)]


def kinds(events: Sequence[Event]) -> list[str]:
    return [e.type for e in events]

"""The sandbox provider is part of the pinned config: a thread continued with another
provider is refused before anything attaches to, creates or dispatches in a sandbox."""

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import JsonValue

from threads import Completed, ConfigError, agent, scripted_model, sqlite
from threads.log import Permissions
from threads.result import Err, Ok
from threads.sandbox.fake import FakeSandbox, SandboxScript
from threads.sandbox.protocol import SandboxContext, SandboxError, SandboxSession

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BYPASS = Permissions(
    mode="bypass",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=True,
    plan_exit_mode="default",
)


@dataclass
class Counting(FakeSandbox):
    attaches: int = 0

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        self.attaches += 1
        return await super().attach(ref, context)


def write(content: str) -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": f"call_{content}",
        "name": "write",
        "input": {"path": "a.txt", "content": content},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def done() -> JsonValue:
    content: JsonValue = [{"type": "text", "text": "Done."}]
    return {"content": content, "stop_reason": "end_turn", "usage": USAGE}


def test_a_thread_continued_with_another_sandbox_provider_is_refused_first() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        first = Counting(SandboxScript())
        bot = agent(
            model=scripted_model({"responses": [write("A"), done()]}),
            sandbox=first,
            permissions=BYPASS,
        )
        started = await bot.run("go", store=store)
        assert isinstance(started, Completed)
        other = Counting(SandboxScript(), provider="other")
        again = agent(
            model=scripted_model({"responses": [write("B"), done()]}),
            sandbox=other,
            permissions=BYPASS,
        )
        with pytest.raises(ConfigError) as raised:
            await again.run("more", store=store, thread=started.thread)
        assert raised.value.code == "invalid_config"
        assert (other.creates, other.attaches) == (0, 0)

    asyncio.run(main())


def test_the_same_provider_reattaches_the_branch_sandbox() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        box = Counting(SandboxScript())

        first = agent(
            model=scripted_model({"responses": [write("A"), done()]}),
            sandbox=box,
            permissions=BYPASS,
        )
        started = await first.run("go", store=store)
        assert isinstance(started, Completed)
        second = agent(
            model=scripted_model({"responses": [write("B"), done()]}),
            sandbox=box,
            permissions=BYPASS,
        )
        continued = await second.run("more", store=store, thread=started.thread)
        assert isinstance(continued, Completed)
        assert box.attaches == 1

    asyncio.run(main())

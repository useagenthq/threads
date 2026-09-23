"""Effects on the live path: a dispatch in doubt is settled by its class's proof, under
the same effect key, or it parks. Nothing repeats without a proof."""

import asyncio

from corpus import Clock
from kit import T0, Tools, kinds, open_store, spec, start, text, use

from threads.loop.drive import drive
from threads.loop.runtime import Idle, Parked
from threads.loop.scripted import scripted_model
from threads.loop.tools import Output, Uncertain

TAIL = ["model_request", "model_response", "turn_completed"]
SENT_TWICE = 2
"""The first attempt, and its deduped re-send under the same key."""


def test_an_idempotent_timeout_inside_its_window_is_resent_under_the_same_key() -> None:
    async def main() -> tuple[list[str], Tools]:
        clock = Clock(T0)
        tools = Tools({"charge": [Uncertain("timeout"), Output("charged")]}, clock)
        model = scripted_model({"responses": [use("charge"), text("Done.")]})
        store = await open_store()
        rt = await start(store, [spec("charge", "idempotent", 60_000)], model, tools, clock)
        before = rt.fold.seq
        assert isinstance(await drive(rt), Idle)
        return kinds([e for e in rt.events if e.seq > before]), tools

    appended, tools = asyncio.run(main())
    assert appended[appended.index("tool_call") :] == [
        "tool_call",
        "permission_decision",
        "effect_begin",
        "effect_unknown",
        "effect_resolved",
        "effect_begin",
        "effect_commit",
        "tool_result",
        *TAIL,
    ]
    assert tools.dispatches["charge"] == SENT_TWICE
    assert len(set(tools.keys)) == 1


def test_an_unguarded_timeout_parks_and_is_never_resent() -> None:
    async def main() -> tuple[object, Tools]:
        clock = Clock(T0)
        tools = Tools({"email": [Uncertain("timeout")]}, clock)
        model = scripted_model({"responses": [use("email")]})
        store = await open_store()
        rt = await start(store, [spec("email", "unguarded")], model, tools, clock)
        return await drive(rt), tools

    halt, tools = asyncio.run(main())
    assert isinstance(halt, Parked)
    assert halt.reason == "effect_unknown"
    assert tools.dispatches["email"] == 1

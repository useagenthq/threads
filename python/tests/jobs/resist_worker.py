"""A `run_sync` script whose tool ignores cancellation (the Ctrl-C drill).

`python resist_worker.py first <dir>`: starts a thread whose one tool call prints
`at tool <thread id>`, records that it ran in `<dir>/ran`, then keeps sleeping through every
cancellation. The parent interrupts it and finally kills it.

`python resist_worker.py again <dir> <thread id>`: runs that thread again and prints the
result's status, then `ran <n>` (how many times the tool body ran) and `begun <n>` (how many
effect_begin events the log holds for the call).
"""

import asyncio
import sys
from pathlib import Path

from jobs.stores import drill_store
from pydantic import BaseModel, JsonValue

from threads import Failed, RunContext, agent, open_thread, scripted_model, tool
from threads.log import EffectBeginEvent, Permissions, ThreadId
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BYPASS = Permissions.model_validate(
    {
        "mode": "bypass",
        "allow": [],
        "ask": [],
        "deny": [],
        "protected_paths": [],
        "allow_bypass": False,
        "plan_exit_mode": "default",
    }
)


class Nothing(BaseModel):
    pass


def _bot(where: Path):  # noqa: ANN202 - a test script
    async def resist(_args: Nothing, ctx: RunContext[None]) -> str:
        with (where / "ran").open("a") as ran:
            ran.write("x\n")
        print(f"at tool {ctx.thread_id}", flush=True)
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                print("resisting", flush=True)

    call: JsonValue = {"type": "tool_use", "call_id": "c1", "name": "resist", "input": {}}
    done: JsonValue = {"type": "text", "text": "done"}
    replies: list[JsonValue] = [
        {"content": [call], "stop_reason": "tool_use", "usage": USAGE},
        {"content": [done], "stop_reason": "end_turn", "usage": USAGE},
    ]
    resisting = tool(
        name="resist", description="Never stops.", input=Nothing, execute=resist, effect="unguarded"
    )
    return agent(
        model=scripted_model({"responses": replies}), tools=[resisting], permissions=BYPASS
    )


def main(role: str, where: Path, thread: str = "") -> None:
    store = drill_store(where)
    bot = _bot(where)
    if role == "first":
        bot.run_sync("go", store=store, deps=None)
        return
    opened = asyncio.run(open_thread(store, ThreadId(thread)))
    assert isinstance(opened, Ok)
    result = bot.run_sync("again", thread=opened.value, deps=None)
    print(result.error.code if isinstance(result, Failed) else result.status, flush=True)
    ran = len((where / "ran").read_text().splitlines())
    timeline = asyncio.run(opened.value.timeline())
    assert isinstance(timeline, Ok)
    begun = [e for e in timeline.value.entries if isinstance(e.event, EffectBeginEvent)]
    print(f"ran {ran}", flush=True)
    print(f"begun {len(begun)}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), *sys.argv[3:])

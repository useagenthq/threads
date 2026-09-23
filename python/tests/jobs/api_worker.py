"""A crash-drill host process for runs started through the run API.

`python api_worker.py api <dir>` runs a host on the drill's file-backed store. With
`DRILL_START=1` it starts one API run; without it, it starts nothing and only recovers what the
store holds. The agent calls `charge` (a host tool with an effect) when `DRILL_CHARGE=1`, then
answers "done". It stops once the run's branch settles: its turn closed, or parked.
`DRILL_STOP_AT`, `DRILL_GO` and the JSONL records work as in worker.py; `charge` records each
call in `charges.jsonl`.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Final

from jobs.worker import DONE, reached, record, started, until
from pydantic import BaseModel, JsonValue

from threads import RunContext, agent, scripted_model, sqlite, tool
from threads._generated.host_api_v1 import StartRunRequest
from threads.agents.store import open_store, scoped
from threads.host import host
from threads.log import BranchId, ModelResponseEvent, Permissions, Principal
from threads.loop.guard import block_model_requests
from threads.loop.model import ModelRequest
from threads.reduce import Fold
from threads.result import Ok
from threads.store import SqliteStore
from threads.store.sql import text_of

TENANT: Final = "acme"
USER: Final = Principal(issuer="api", tenant=TENANT, subject="alice")
CHARGE: Final[JsonValue] = {
    "content": [{"type": "tool_use", "call_id": "c1", "name": "charge", "input": {}}],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
ALLOW_CHARGE: Final = Permissions(
    mode="default",
    allow=["charge"],
    ask=[],
    deny=[],
    protected_paths=[".git/**"],
    allow_bypass=False,
    plan_exit_mode="default",
)


class Empty(BaseModel):
    pass


async def read(sq: SqliteStore) -> Fold | None:
    """The drill run's branch, read back from the log; None before the run is durable."""
    found = await sq.run(lambda c: c.execute("SELECT branch_id FROM run_receipts").fetchone())
    if found is None:
        return None
    log = await sq.read(BranchId(text_of(found[0])), 0)
    return log.value.fold if isinstance(log, Ok) else None


async def _settled(sq: SqliteStore) -> bool:
    fold = await read(sq)
    return fold is not None and (not fold.in_turn or bool(fold.parked))


async def serve(where: Path) -> None:
    if os.environ.get("DRILL_GO") == "1":
        await started(where)
    store = sqlite(str(where))
    sq = await open_store(scoped(store, TENANT))
    before = await read(sq)
    # A restart answers only what the log has not: the script is the model's, not the process's.
    answered = (
        0 if before is None else sum(isinstance(e, ModelResponseEvent) for e in before.events)
    )
    script: list[JsonValue] = [CHARGE, DONE] if os.environ.get("DRILL_CHARGE") == "1" else [DONE]
    model = scripted_model({"responses": script[answered:]})

    def played(_request: ModelRequest) -> None:
        record(where / "model.jsonl", {})
        reached("model_request", where)

    model.before_send = played

    async def charge(_args: Empty, _ctx: RunContext[None]) -> str:
        record(where / "charges.jsonl", {})
        reached("effect_begin", where)
        return "charged"

    card = tool(name="charge", description="Charge.", input=Empty, runs="host", execute=charge)
    bot = agent(model=model, tools=[card], permissions=ALLOW_CHARGE)
    async with host(store=store, agents={"bot": bot}) as served:
        if os.environ.get("DRILL_START") == "1":
            request = StartRunRequest.model_validate({"agent": "bot", "input": "Charge me."})
            accepted = await served.start_run(request, principal=USER, idempotency_key="drill")
            assert isinstance(accepted, Ok), accepted
        await until(lambda: _settled(sq))


if __name__ == "__main__":
    block_model_requests()
    asyncio.run(serve(Path(sys.argv[2])))

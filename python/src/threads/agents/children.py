"""A child thread's terminal result as its parent records it: `agent_finished{status, usage}`
. Usage is the child's aggregate, its own children's included, with an
unknown total as null."""

from typing import assert_never

from pydantic import JsonValue

from threads.agents.results import (
    BudgetExhausted,
    Cancelled,
    Completed,
    Failed,
    HandedOff,
    Parked,
    RunResult,
)
from threads.agents.store import now_ms
from threads.log import AgentFinishedEvent, ThreadId
from threads.result import Err
from threads.store import SqliteStore


def _status(result: RunResult[str]) -> tuple[str, str | None]:
    """The terminal status and the output the parent sees (None: nothing to report)."""
    match result:
        case Completed(output=output):
            return "completed", output
        case BudgetExhausted(budget=budget):
            return "budget_exhausted", f"budget exhausted: {budget.limit}"
        case Cancelled():
            return "cancelled", None
        case Failed(error=error):
            return "failed", f"failed: {error.code}: {error.message}"
        case Parked(reason=reason):
            # ponytail: a child can't park its parent yet; it ends failed and keeps its log.
            return "failed", f"failed: the subagent parked ({reason})"
        case HandedOff():
            return "failed", "failed: a subagent can't hand off"
        case _:
            assert_never(result)


async def finished(
    sq: SqliteStore, child: ThreadId, result: RunResult[str]
) -> tuple[dict[str, JsonValue], str | None]:
    status, output = _status(result)
    usage: dict[str, JsonValue] = {"input_tokens": None, "output_tokens": None}
    read = await sq.read(result.thread.branch, now_ms())
    if not isinstance(read, Err):
        fold = read.value.fold
        known = fold.unknown_responses == 0
        inputs, outputs = fold.input_tokens, fold.output_tokens
        for event in fold.events:
            if isinstance(event, AgentFinishedEvent):
                grand = event.data.usage
                known = known and grand.input_tokens is not None and grand.output_tokens is not None
                inputs += grand.input_tokens or 0
                outputs += grand.output_tokens or 0
        if known:
            usage = {"input_tokens": inputs, "output_tokens": outputs}
    data: dict[str, JsonValue] = {"child_thread_id": child, "status": status, "usage": usage}
    return data, output

"""The prefix of a simulated case (spec lane 32, B.1): the real turns before the saved one.
Continue them when the agent's config_hash is unchanged -- the case log is imported and the
conversation goes on, so the prefix costs no model call -- and re-drive their inputs when it
changed, so a changed agent is graded on the whole path."""

from dataclasses import dataclass
from typing import Literal

from threads.agents.definition import dry_pin
from threads.agents.results import Thread
from threads.agents.store import open_store
from threads.evals.case_dir import CaseDir
from threads.evals.checks import CaseLog
from threads.evals.ledger import cost_bound
from threads.evals.live_env import Env, EvalAgent
from threads.log import ThreadStartedEvent, UserInputEvent
from threads.result import Ok
from threads.store import verify_export


@dataclass(frozen=True, slots=True)
class Plan:
    mode: Literal["continued", "redriven", "none"]
    redrive: tuple[str, ...] = ()
    """Recorded inputs to re-send before the opener; empty unless the prefix is re-driven."""
    turns: int = 0
    thread: Thread | None = None
    """The imported thread to continue; None when the agent starts a new one."""
    since: int = 0
    """Events at or below this seq are the imported prefix, not this conversation's."""
    base_cost_nanos: int = 0


def prefix_texts(log: CaseLog) -> tuple[str | None, ...]:
    """The recorded user inputs before the saved turn, in log order. A content-part
    input has no text, and a live run skips the case."""
    return tuple(
        e.data.text if isinstance(e.data.text, str) else None
        for e in log.events
        if isinstance(e, UserInputEvent)
    )


def _recorded_hash(log: CaseLog) -> str | None:
    started = next((e for e in log.events if isinstance(e, ThreadStartedEvent)), None)
    return None if started is None else started.data.config_hash


async def _import_case(case: CaseDir, env: Env) -> Plan | None:
    """Imports the case log into the eval store, so the conversation continues the real thread."""
    verified = verify_export(case.log, case.meta.clock.now)
    if not isinstance(verified, Ok):
        return None
    sq = await open_store(env.store)
    for artifact in case.artifacts:
        await sq.put_artifact(artifact)
    imported = await sq.import_log(verified.value)
    if not isinstance(imported, Ok):
        return None
    header = verified.value.segments[-1].header
    thread = Thread(header.thread_id, header.branch_id, env.store)
    return Plan(
        "continued",
        turns=0,
        thread=thread,
        since=verified.value.fold.seq,
        base_cost_nanos=await cost_bound(thread),
    )


async def plan_prefix(case: CaseDir, log: CaseLog, target: EvalAgent, env: Env) -> Plan:
    texts = tuple(t for t in prefix_texts(log) if t is not None)
    if not texts:
        return Plan("none")
    same = dry_pin(target.definition).started.get("config_hash") == _recorded_hash(log)
    imported = await _import_case(case, env) if same else None
    if imported is None:
        return Plan("redriven", redrive=texts, turns=len(texts))
    return Plan(
        "continued",
        turns=len(texts),
        thread=imported.thread,
        since=imported.since,
        base_cost_nanos=imported.base_cost_nanos,
    )

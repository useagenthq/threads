"""`run_evals()` (spec/api.json `runEvals`): every saved case under `cases`, sorted by name,
through the checks cheapest first. Offline it makes no model call and touches no store the caller
passed; live, it runs the current agents and a judge under a budget. Per-case outcomes, and a
guard abort, are values; only a setup mistake (ConfigError) or a bug raises."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from threads._generated.eval_v1 import EvalReport
from threads.agents.config import ConfigError
from threads.agents.definition import DryPin, dry_pin
from threads.agents.store import Store, scoped, sqlite
from threads.evals.case_dir import CaseDir, case_names, read_case
from threads.evals.checks import (
    CaseLog,
    drift_check,
    drift_reason,
    replay_check,
    rerun_check,
    rerun_reason,
    skip_reason,
)
from threads.evals.drift import DriftResult
from threads.evals.live import Env, EvalAgent, Live, Outcome, live_check
from threads.evals.live_env import Simulation
from threads.evals.report import Totals, add_cost, report
from threads.log import Cost, ThreadStartedEvent
from threads.result import Err

type Case = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Plan:
    """What one run of the evals checks against."""

    root: Path
    pins: tuple[DryPin, ...] | None
    """The dry pins of the agents given; None: no drift check."""
    agents: tuple[EvalAgent, ...] = ()
    live: Live | None = None
    store: Store | None = None
    kept: bool = False


def _result(
    name: str,
    status: str,
    checks: Mapping[str, JsonValue],
    reason: str | None = None,
    simulation: Simulation | None = None,
) -> Case:
    out: Case = {"name": name, "status": status}
    if reason is not None:
        out["reason"] = reason
    if simulation is not None:
        out["simulation"] = simulation.to_json()
    out["checks"] = dict(checks)
    return out


def _check_options(root: Path, live: Live | None, agents: Sequence[EvalAgent] | None) -> None:
    if not root.is_dir():
        why = f"cases: no directory {root}; save one with thread.save_case() or pass cases"
        raise ConfigError("invalid_config", why)
    if live is not None and not agents:
        raise ConfigError("invalid_config", "live evals need agents: pass agents")


def _failing(check: JsonValue) -> JsonValue:
    verdicts = check.get("verdicts") if isinstance(check, dict) else None
    for v in verdicts if isinstance(verdicts, list) else []:
        if isinstance(v, dict) and v.get("pass") is not True:
            return v.get("criterion")
    return None


def _judged(name: str, checks: Mapping[str, JsonValue], live: Outcome) -> Case:
    """The live check's part of a case: its status, reason and judge result."""
    if live.kind == "blocked":
        return _result(name, "error", checks, "model_blocked")
    if live.kind != "graded":
        return _result(name, live.kind, checks, live.reason, live.simulation)
    graded = {**checks, "judge": live.check}
    failing = _failing(live.check)
    if failing is None:
        return _result(name, "passed", graded, None, live.simulation)
    return _result(name, "failed", graded, f"judge: criterion {failing} failed", live.simulation)


async def _live(plan: Plan, log: CaseLog, case: CaseDir) -> Outcome | None:
    started = next((e for e in log.events if isinstance(e, ThreadStartedEvent)), None)
    name = "" if started is None else started.data.agent_name
    target = next((a for a in plan.agents if a.name == name), None)
    if plan.live is None or plan.store is None or target is None:
        return None
    pinned = dry_pin(target.definition)
    # A lead's members run in the team worker, where the case's stubs can't reach: never live.
    if pinned.leads_team:
        return Outcome("skipped", reason="live_not_runnable:team_calls")
    fresh = "sandbox_provider" in pinned.started
    env = Env(plan.live, plan.store, plan.kept, fresh)
    return await live_check(case, log, target, env)


def _stale(case: Case, drift: DriftResult) -> Case:
    return {**case, "status": "stale", "reason": drift_reason(drift)}


async def _offline(case: CaseDir) -> tuple[Case, CaseLog | None, str | None]:
    """Replay, then the rerun unless the case can't rerun offline: a final result when a check
    failed, else the checks so far, the case log and the skip reason."""
    replay, log = replay_check(case)
    checks: Case = {"replay": replay}
    if log is None:
        return _result(case.name, "failed", checks, f"replay: {replay.get('code')}"), None, None
    skip = skip_reason(case, log)
    if skip is None:
        rerun = await rerun_check(case)
        if isinstance(rerun, str):
            return _result(case.name, "error", checks, rerun), None, None
        checks["rerun"] = rerun
        if rerun.get("ok") is not True:
            return _result(case.name, "failed", checks, rerun_reason(rerun)), None, None
    return checks, log, skip


async def evaluate(plan: Plan, name: str) -> tuple[Case, Outcome | None]:
    """One case through every check it needs, and its live outcome when it ran live."""
    read = read_case(plan.root, name)
    if isinstance(read, Err):
        return _result(name, "error", {}, f"unreadable: {read.error}"), None
    checks, log, skip = await _offline(read.value)
    if log is None:
        return checks, None
    drift = None if plan.pins is None else drift_check(read.value, log, plan.pins)
    if drift is not None:
        checks["drift"] = drift.to_json()
    live = await _live(plan, log, read.value)
    if live is not None:
        judged = _judged(name, checks, live)
        stale = judged["status"] == "passed" and drift is not None and not drift.ok
        return (_stale(judged, drift) if stale and drift is not None else judged), live
    if skip is not None:
        return _result(name, "skipped", checks, skip), None
    if drift is not None and not drift.ok:
        return _result(name, "stale", checks, drift_reason(drift)), None
    return _result(name, "passed", checks), None


async def evals_of(plan: Plan, names: Sequence[str], *, strict: bool = False) -> EvalReport:
    """The cases in order; a guard block stops the run and leaves the rest not_run."""
    cases: list[Case] = []
    agent_calls = user_calls = judge_calls = 0
    cost: Cost | None = None
    priced = False
    aborted: dict[str, JsonValue] | None = None
    for name in names:
        if aborted is not None:
            cases.append(_result(name, "not_run", {}))
            continue
        case, live = await evaluate(plan, name)
        cases.append(case)
        if live is not None and live.kind == "blocked":
            aborted = {"code": "model_blocked", "case": name, "model": live.reason}
        elif live is not None:
            agent_calls += live.agent_calls
            user_calls += live.user_calls
            judge_calls += live.judge_calls
            cost = add_cost(cost, live.cost, first=not priced)
            priced = True
    totals = Totals(
        agent_calls,
        user_calls,
        judge_calls,
        cost,
        live=plan.live is not None,
        agents=plan.pins is not None,
        strict=strict,
        aborted=aborted,
    )
    return report(cases, totals)


async def run_evals(  # noqa: PLR0913 - spec/api.json runEvals options
    *,
    cases: str = "cases",
    only: Sequence[str] | None = None,
    agents: Sequence[EvalAgent] | None = None,
    live: Live | None = None,
    store: Store | None = None,
    strict: bool = False,
) -> EvalReport:
    """Runs every saved case's checks and returns the report (spec/api.json runEvals). `agents`
    adds the free drift check; `live` grades them with a judge model, under `live.budget`."""
    root = Path(cases)
    _check_options(root, live, agents)
    given = tuple(agents or ())
    plan = Plan(
        root,
        None if agents is None else tuple(dry_pin(a.definition) for a in given),
        given,
        live,
        scoped(store or sqlite(":memory:"), "evals"),
        store is not None,
    )
    names = [n for n in case_names(root) if only is None or n in only]
    _check_user(root, names, live)
    return await evals_of(plan, names, strict=strict)


def _check_user(root: Path, names: Sequence[str], live: Live | None) -> None:
    """A model-kind simulated case needs live.user, before the first model call (32 E)."""
    if live is None or live.user is not None:
        return
    for name in names:
        read = read_case(root, name)
        if isinstance(read, Err) or read.value.meta.simulate is None:
            continue
        if read.value.meta.simulate.kind == "model":
            why = f"case {name} simulates a user with a model: set live.user"
            raise ConfigError("invalid_config", why)

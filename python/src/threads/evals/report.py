"""The report run_evals returns (spec lane 22, D): counts, the one summary line the CLI prints, and
whether the run passed. No timestamps or durations, so an offline report is byte-stable and the
same in both languages."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from threads._generated.eval_v1 import EvalReport
from threads.evals.compare import canonical
from threads.log import Cost

_FRAMEWORK_ONLY: Final = " (framework checks only; pass --agent to detect changes to your agents)"


@dataclass(frozen=True, slots=True)
class Totals:
    agent_calls: int
    user_calls: int
    judge_calls: int
    cost: Cost | None
    live: bool
    agents: bool
    """Drift ran: agents were given."""
    strict: bool
    aborted: Mapping[str, JsonValue] | None = None


def add_cost(total: Cost | None, part: Cost | None, *, first: bool) -> Cost | None:
    """Two costs added; None once either part ran unpriced. `first`: `total` has no part yet."""
    if first:
        return part
    if total is None or part is None:
        return None
    return Cost(
        currency=total.currency,
        known_nanos=total.known_nanos + part.known_nanos,
        upper_bound_nanos=total.upper_bound_nanos + part.upper_bound_nanos,
        complete=total.complete and part.complete,
        bounded=total.bounded and part.bounded,
    )


def dollars(nanos: int) -> str:
    """Nano-dollars as dollars and cents, rounded half up."""
    cents = (nanos + 5_000_000) // 10_000_000
    return f"${cents // 100}.{cents % 100:02d}"


def _count(cases: Sequence[Mapping[str, JsonValue]], status: str) -> int:
    return sum(1 for c in cases if c.get("status") == status)


def _simulations(cases: Sequence[Mapping[str, JsonValue]]) -> str:
    """`<name>: <messages> messages, <ended>` for each simulated case (lane 32, E)."""
    out = ""
    for c in cases:
        sim = c.get("simulation")
        if not isinstance(sim, dict):
            continue
        ended = str(sim.get("ended", "")).replace("_", " ", 1)
        out += f"; {c.get('name')}: {sim.get('messages')} messages, {ended}"
    return out


def _summary(counts: Mapping[str, int], t: Totals, cases: Sequence[Mapping[str, JsonValue]]) -> str:
    parts = [f"{counts['passed']} passed", f"{counts['failed']} failed"]
    if counts["stale"]:
        parts.append(f"{counts['stale']} stale")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped")
    if counts["errors"]:
        parts.append(f"{counts['errors']} error{'' if counts['errors'] == 1 else 's'}")
    if counts["not_run"]:
        parts.append(f"{counts['not_run']} not run")
    line = ", ".join(parts)
    if t.live:
        spent = "cost unknown" if t.cost is None else dollars(t.cost.known_nanos)
        calls = t.agent_calls + t.user_calls + t.judge_calls
        line += (
            f"; {calls} model calls ({t.agent_calls} agent, "
            f"{t.user_calls} user, {t.judge_calls} judge), {spent}"
        )
    line += _simulations(cases)
    if t.aborted is not None:
        line += f"; aborted: {t.aborted.get('code')} ({t.aborted.get('model')})"
    return line if t.agents else line + _FRAMEWORK_ONLY


def report(cases: Sequence[Mapping[str, JsonValue]], t: Totals) -> EvalReport:
    counts = {
        "passed": _count(cases, "passed"),
        "failed": _count(cases, "failed"),
        "stale": _count(cases, "stale"),
        "skipped": _count(cases, "skipped"),
        "errors": _count(cases, "error"),
        "not_run": _count(cases, "not_run"),
    }
    failed = counts["failed"] + counts["errors"] + counts["not_run"] > 0
    strictly = t.strict and counts["stale"] + counts["skipped"] > 0
    body: dict[str, JsonValue] = {
        "format": "threads-eval",
        "format_version": 1,
        "summary": _summary(counts, t, cases),
        "ok": not failed and not strictly and t.aborted is None,
        **counts,
        "model_calls": {
            "agent": t.agent_calls,
            "user": t.user_calls,
            "judge": t.judge_calls,
        },
        "cost": None if t.cost is None else t.cost.model_dump(mode="json"),
        "cases": [dict(c) for c in cases],
    }
    if t.aborted is not None:
        body["aborted"] = dict(t.aborted)
    return EvalReport.model_validate_json(canonical(body))


_LABELS: Final = {
    "passed": "PASS",
    "failed": "FAIL",
    "stale": "STALE",
    "skipped": "SKIP",
    "error": "ERROR",
    "not_run": "NOT RUN",
}


def case_line(case: Mapping[str, JsonValue]) -> str:
    """One line per case, as the CLI prints them."""
    checks = case.get("checks")
    drift = checks.get("drift") if isinstance(checks, dict) else None
    listed = drift.get("unchecked") if isinstance(drift, dict) else None
    unchecked = ", ".join(str(u) for u in listed) if isinstance(listed, list) else None
    head = f"{_LABELS[str(case.get('status'))]} {case.get('name')}"
    reason = case.get("reason")
    # After a reason: "STALE c drift: model; unchecked mcp:jira". Alone: "PASS c (unchecked ...)".
    if reason is None:
        return head if unchecked is None else f"{head} (drift: unchecked {unchecked})"
    return f"{head} {reason}" + ("" if unchecked is None else f"; unchecked {unchecked}")

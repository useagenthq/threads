"""`threads eval` (spec/api.json cli): run_evals() over saved cases. Without --agent it loads no
user code and runs the framework checks; --agent adds drift; --live adds the judged run."""

import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TypeGuard

from pydantic import TypeAdapter, ValidationError

from threads.agents.agent import Agent
from threads.agents.config import ConfigError
from threads.agents.store import sqlite
from threads.cli.serve import import_target
from threads.evals.case_dir import case_names
from threads.evals.compare import canonical
from threads.evals.live import EvalAgent, Live
from threads.evals.report import case_line
from threads.evals.run import run_evals
from threads.log import Budget
from threads.loop.model import Model
from threads.reduce.handlers import to_json

_RUBRIC = TypeAdapter[list[str]](list[str])
_OBJECTS = TypeAdapter[list[object]](list[object])


@dataclass(frozen=True, slots=True)
class EvalArgs:
    agent: str | None
    cases: str
    only: tuple[str, ...]
    live: bool
    store: str | None
    strict: bool
    out: str | None


@dataclass(frozen=True, slots=True)
class _Loaded:
    agents: tuple[EvalAgent, ...]
    live: Live | None = None


def _is_model(value: object) -> TypeGuard[Model]:
    return callable(getattr(value, "send", None)) and hasattr(value, "info")


def _agents(module: ModuleType) -> tuple[EvalAgent, ...]:
    found: object = getattr(module, "agents", None)
    if found is None:
        found = getattr(module, "agent", None)
    many = _OBJECTS.validate_python(found) if isinstance(found, list | tuple) else [found]
    return tuple(a for a in many if _is_agent(a))


def _is_agent(value: object) -> TypeGuard[EvalAgent]:
    return isinstance(value, Agent)


def _live(module: ModuleType, path: str, agents: tuple[EvalAgent, ...]) -> _Loaded | str:
    judge: object = getattr(module, "judge", None)
    if not _is_model(judge):
        return f"export judge from {path}"
    budget: object = getattr(module, "budget", None)
    if not isinstance(budget, Budget):
        return f"export budget from {path} (a Budget, such as Budget(max_cost_nanos=500_000_000))"
    rubric: object = getattr(module, "rubric", ())
    try:
        criteria = _RUBRIC.validate_python(rubric)
    except ValidationError:
        return f"rubric in {path} must be a list of criteria"
    return _Loaded(agents, Live(judge, budget, tuple(criteria)))


def _load(args: EvalArgs) -> _Loaded | str:
    if args.agent is None:
        return "threads eval --live needs --agent <module>" if args.live else _Loaded(())
    try:
        module = import_target(args.agent)
    except Exception as error:
        return f"{args.agent}: import failed: {error}"
    agents = _agents(module)
    if not agents:
        return f"define agents (a list) or agent in {args.agent}"
    return _live(module, args.agent, agents) if args.live else _Loaded(agents)


async def evals(args: EvalArgs) -> int:
    loaded = _load(args)
    if isinstance(loaded, str):
        print(loaded, file=sys.stderr)
        return 2
    live = loaded.live
    if live is not None and Path(args.cases).is_dir():
        n = len([c for c in case_names(Path(args.cases)) if not args.only or c in args.only])
        budget = canonical(to_json(live.budget))
        print(f"live: {n} cases, up to {2 * n} model runs (agent + judge), budget {budget} per run")
    try:
        report = await run_evals(
            cases=args.cases,
            only=args.only or None,
            agents=None if args.agent is None else loaded.agents,
            live=live,
            store=None if args.store is None else sqlite(args.store),
            strict=args.strict,
        )
    except ConfigError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 2
    for case in report.cases:
        wire = to_json(case)
        print(case_line(wire if isinstance(wire, dict) else {}))
    if live is not None and args.store is None:
        print("judge threads were not kept; pass --store to keep them")
    print(report.summary)
    if args.out is not None:
        Path(args.out).write_text(canonical(to_json(report)) + "\n", encoding="utf-8")
    return 0 if report.ok else 1

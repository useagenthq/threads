"""`threads eval` (spec/api.json cli): run_evals() over saved cases. Without --agent it loads no
user code and runs the framework checks; --agent adds drift; --live adds the judged run."""

import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Annotated, ClassVar, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from threads._generated.eval_v1 import CaseSimulate
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


class _Simulated(BaseModel):
    """Only the `simulate` of a case.json, for the preflight line."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    simulate: Annotated[CaseSimulate, Field(discriminator="kind")] | None = None


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
    user: object = getattr(module, "user", None)
    if user is not None and not _is_model(user):
        return f"user in {path} must be a model"
    return _Loaded(agents, Live(judge, budget, tuple(criteria), user))


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


_SIMULATE: TypeAdapter[_Simulated] = TypeAdapter(_Simulated)


def _simulations(args: EvalArgs) -> list[CaseSimulate]:
    """The `simulate` of each case, read off case.json: what the preflight line counts."""
    root = Path(args.cases)
    out: list[CaseSimulate] = []
    for name in case_names(root):
        if args.only and name not in args.only:
            continue
        try:
            got = _SIMULATE.validate_json((root / name / "case.json").read_bytes())
        except (ValidationError, OSError):
            continue
        if got.simulate is not None:
            out.append(got.simulate)
    return out


def _preflight(args: EvalArgs, live: Live) -> int | None:
    """What a live run will cost, printed before the first model call; 2 when a model is missing."""
    n = len([c for c in case_names(Path(args.cases)) if not args.only or c in args.only])
    sims = _simulations(args)
    if any(s.kind == "model" for s in sims) and live.user is None:
        print(f"export user from {args.agent or './agents.py'}", file=sys.stderr)
        return 2
    budget = canonical(to_json(live.budget))
    if not sims:
        print(f"live: {n} cases, up to {2 * n} model runs (agent + judge), budget {budget} per run")
        return None
    messages = sum(
        (s.max_messages if isinstance(s.max_messages, int) else 5)
        if s.kind == "model"
        else len(s.messages) + 1
        for s in sims
    )
    print(
        f"live: {n} cases ({len(sims)} simulated, up to {messages} user messages), "
        f"budget {budget} per conversation and per judge run"
    )
    return None


async def evals(args: EvalArgs) -> int:
    loaded = _load(args)
    if isinstance(loaded, str):
        print(loaded, file=sys.stderr)
        return 2
    live = loaded.live
    if live is not None and Path(args.cases).is_dir():
        stop = _preflight(args, live)
        if stop is not None:
            return stop
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

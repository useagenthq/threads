"""Whether a run may omit deps: every app tool's context parameter is annotated
`RunContext[None]` (spec/api.json Agent.run deps: "omitted, tools see null").

Only the context parameter's annotation is read, so an unresolvable name elsewhere in a tool's
signature (a local class under `from __future__ import annotations`, a TYPE_CHECKING-only
import) doesn't matter, and a return annotation never counts."""

import functools
import inspect
from collections.abc import Callable, Sequence
from typing import Literal, TypeAliasType, get_args, get_origin

from threads.agents.context import RunContext

type ContextDeps = Literal["none", "deps", "unreadable"]
"""none: `RunContext[None]`. deps: any other context type. unreadable: no annotation to read."""


def context_deps(execute: object) -> ContextDeps:
    """What `execute(input, ctx)` annotates `ctx` with, through partials, `functools.wraps`
    and `type` aliases."""
    if not callable(execute):
        return "unreadable"
    try:
        signature = inspect.signature(execute)  # follows partial and __wrapped__
    except (TypeError, ValueError):
        return "unreadable"
    positional = [
        p
        for p in signature.parameters.values()
        if p.kind in {p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD}
    ]
    if len(positional) < 2 or positional[1].annotation is inspect.Parameter.empty:  # noqa: PLR2004 - (input, ctx)
        return "unreadable"
    hint = _resolved(positional[1].annotation, _globals(execute))
    while isinstance(hint, TypeAliasType):
        hint = hint.__value__
    if hint is None:
        return "unreadable"
    none = get_origin(hint) is RunContext and get_args(hint) == (type(None),)
    return "none" if none else "deps"


def missing_deps(agent_name: str, tools: Sequence[tuple[str, ContextDeps]]) -> str:
    """The ConfigError message for a run without deps, naming what to do."""
    unreadable = [name for name, deps in tools if deps == "unreadable"]
    if unreadable:
        return (
            f"agent {agent_name}: couldn't read the context annotation of tool "
            f"{', '.join(unreadable)}; pass run(..., deps=...) (deps=None if it takes none)"
        )
    return f"agent {agent_name} needs deps for its tools: pass run(..., deps=...)"


def _globals(execute: Callable[..., object]) -> dict[str, object]:
    """The module namespace a string annotation resolves in: the innermost wrapped function's."""
    fn: object = execute
    while True:
        if isinstance(fn, functools.partial):
            fn = fn.func
        elif inspect.ismethod(fn):
            fn = fn.__func__
        elif (inner := getattr(fn, "__wrapped__", None)) is not None:
            fn = inner
        else:
            break
    return dict(fn.__globals__) if inspect.isfunction(fn) else {}


def _resolved(annotation: object, namespace: dict[str, object]) -> object:
    """The annotation as a value; a string one (future annotations) is evaluated like
    get_type_hints does, or None when a name in it doesn't resolve."""
    if not isinstance(annotation, str):
        return annotation
    try:
        return eval(annotation, namespace)  # noqa: S307 - the tool author's own annotation, as get_type_hints does
    except Exception:  # any failure means unreadable
        return None

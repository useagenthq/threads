"""Running hooks: awaited, bounded by the extension's timeout, and turned into
values. A hook that throws, times out or returns something outside its decision union is a
failure (`decision: failed`); what a failure means is its class's rule, applied by the loop."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue, TypeAdapter, ValidationError

from threads.hooks.types import (
    CompactGate,
    HookName,
    InputDecision,
    ModelGate,
    ResponseGate,
    ResultGate,
    StopGate,
    SwitchGate,
    ToolGate,
    wire_name,
)
from threads.loop.drafts import draft
from threads.store import Draft

type Call = Callable[..., Awaitable[object]]
"""One hook with the run's context already bound: it takes the hook point's other arguments."""

INPUT: Final = TypeAdapter[InputDecision](InputDecision)
MODEL: Final = TypeAdapter[ModelGate](ModelGate)
RESPONSE: Final = TypeAdapter[ResponseGate](ResponseGate)
TOOL: Final = TypeAdapter[ToolGate](ToolGate)
RESULT: Final = TypeAdapter[ResultGate](ResultGate)
COMPACT: Final = TypeAdapter[CompactGate](CompactGate)
STOP: Final = TypeAdapter[StopGate](StopGate)
SWITCH: Final = TypeAdapter[SwitchGate](SwitchGate)
TEXTS: Final = TypeAdapter[Sequence[str]](Sequence[str])
NOTHING: Final = TypeAdapter[None](None)


_LATE: set[asyncio.Future[object]] = set()
"""Hooks past their deadline, held until they finish so nothing is left unretrieved."""


def _drop(task: asyncio.Future[object]) -> None:
    _LATE.discard(task)
    if not task.cancelled():
        task.exception()


@dataclass(frozen=True, slots=True)
class Bound:
    """One extension's hooks for one run."""

    extension: str
    timeout_ms: int
    hooks: Mapping[HookName, Call]


@dataclass(frozen=True, slots=True)
class Ran[T]:
    """One hook's answer: its decision, or why it failed."""

    extension: str
    value: T | None
    failure: str | None = None


class HookRunner:
    """The run's extensions, in declaration order."""

    def __init__(self, bound: Sequence[Bound] = ()) -> None:
        self._bound = tuple(bound)

    def has(self, hook: HookName) -> bool:
        return any(hook in b.hooks for b in self._bound)

    def defining(self, hook: HookName) -> tuple[Bound, ...]:
        """The extensions defining `hook`, `before*` in declaration order and `after*` reversed."""
        order = reversed(self._bound) if hook.startswith("after") else iter(self._bound)
        return tuple(b for b in order if hook in b.hooks)

    async def run[T](self, hook: HookName, parse: TypeAdapter[T], *args: object) -> list[Ran[T]]:
        """Every extension's hook in `defining` order, one at a time: a later hook sees the log
        the earlier one led to."""
        return [await run_one(b, hook, parse, *args) for b in self.defining(hook)]


async def run_one[T](bound: Bound, hook: HookName, parse: TypeAdapter[T], *args: object) -> Ran[T]:
    """One extension's hook, bounded by its timeout."""
    task = asyncio.ensure_future(bound.hooks[hook](*args))
    await asyncio.wait({task}, timeout=bound.timeout_ms / 1000)
    if not task.done():
        # The deadline alone decides: cancellation is requested, but a hook that swallows it
        # and answers late is never awaited here, and its late answer is dropped.
        task.cancel()
        _LATE.add(task)
        task.add_done_callback(_drop)
        return Ran(bound.extension, None, f"timed out after {bound.timeout_ms} ms")
    error = task.exception()
    if error is not None:
        # Host hook code failing is a recorded decision, never a crash of the run.
        return Ran(bound.extension, None, f"{type(error).__name__}: {error}")
    try:
        return Ran(bound.extension, parse.validate_python(task.result(), strict=True))
    except ValidationError as error:
        return Ran(bound.extension, None, f"not a {hook} decision: {error.error_count()} error(s)")


def decision_draft(
    hook: HookName, ran: Ran[object], decision: str, reason: str | None = None, **ids: str
) -> Draft:
    """The `hook_decision` event: a failure is `failed` with its reason, whatever was asked."""
    data: dict[str, JsonValue] = {
        "extension": ran.extension,
        "hook": wire_name(hook),
        "decision": "failed" if ran.failure is not None else decision,
    }
    why = ran.failure if ran.failure is not None else reason
    if why:
        data["reason"] = why
    data.update(ids)
    return draft("hook_decision", data)


def injected(extension: str, texts: Sequence[str]) -> list[Draft]:
    """Hook injections: after the declared prefix, inside the untrusted-reference wrapper. Never
    instructions, never line 0."""
    return [
        draft(
            "injected",
            {
                "source": "hook",
                "trust": "untrusted_reference",
                "origin": {"id": extension},
                "text": text,
            },
        )
        for text in texts
    ]

"""The hook-kinds probe (spec lane 22, A.2): the real loop, once per hook, with a no-op extension
that defines only that hook. A run that appends a hook_decision makes the hook "recorded"; one that
appends none makes it "observation". The result must equal HOOK_KINDS, which the eval runner
reads, and a hook added later fails here until it is classified."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Final

from eval_kit import ALLOW, LOOKUP, say, use
from pydantic import JsonValue

from threads import Agent, agent, scripted_model, sqlite
from threads.evals.kinds import HOOK_KINDS
from threads.hooks.extension import extension
from threads.hooks.types import HookName, Hooks, wire_name
from threads.log import Event, HookDecisionEvent, ModelRef, Retry
from threads.loop.scripted import SCRIPTED_INFO, ScriptedModel
from threads.result import Ok
from threads.thread.control import LOCAL_OPERATOR

NOOP: Final[Mapping[HookName, object]] = {
    "session_start": [],
    "session_end": None,
    "before_input": {"decision": "allow"},
    "before_model": {"decision": "proceed"},
    "after_model": {"decision": "proceed"},
    "before_tool": {"decision": "allow"},
    "permission_request": {"decision": "allow"},
    "permission_denied": None,
    "after_tool": [],
    "before_tool_result": {"decision": "proceed"},
    "after_tool_batch": [],
    "before_compact": {"decision": "proceed"},
    "after_compact": [],
    "on_stop": {"decision": "stop"},
    "on_stop_failure": None,
    "subagent_start": {"decision": "allow"},
    "subagent_stop": {"decision": "stop"},
    "before_model_switch": {"decision": "allow"},
    "after_model_switch": None,
    "notification": None,
}
OVERLOADED: JsonValue = {"error": {"reason": "overloaded", "http_status": 529}}
TOOL_TURN: tuple[JsonValue, ...] = (use("lookup_order", {"id": "1"}, "c1"), say("done"))


def _retry(fallback_after: int = 3, max_retries: int = 8) -> Retry:
    return Retry(
        max_retries=max_retries,
        base_delay_ms=1,
        max_delay_ms=1,
        max_retry_after_ms=60_000,
        max_total_wait_ms=600_000,
        crash_resends=2,
        fallback_after=fallback_after,
        fallback_scope="turn",
        heartbeat_ms=15_000,
    )


class _Renamed(ScriptedModel):
    """A scripted fallback under another name, so the switch is a new settings epoch."""

    def __init__(self, responses: list[JsonValue]) -> None:
        inner = scripted_model({"responses": responses})
        super().__init__([], {})
        self._entries = inner._entries
        ref = ModelRef(provider="scripted", name="scripted-small")
        limits = SCRIPTED_INFO.limits.model_copy(update={"name": "scripted-small"})
        self._info = replace(SCRIPTED_INFO, model=ref, limits=limits)


def _bot(hook: HookName, hooks: Hooks[None]) -> Agent[None, str]:
    ext = [extension(name="probe", hooks=hooks)]
    tools = [LOOKUP]

    def model(*replies: JsonValue) -> ScriptedModel:
        return scripted_model({"responses": list(replies)})

    match hook:
        case "before_tool" | "after_tool" | "before_tool_result" | "after_tool_batch":
            perms = ALLOW
        case "permission_request":
            perms = ALLOW.model_copy(update={"allow": [], "ask": ["lookup_order"]})
        case "permission_denied":
            perms = ALLOW.model_copy(update={"allow": [], "deny": ["lookup_order"]})
        case "on_stop_failure":
            return agent(
                name="probe", model=model(OVERLOADED), retry=_retry(max_retries=0), extensions=ext
            )
        case "notification":
            return agent(
                name="probe", model=model(OVERLOADED, say("done")), retry=_retry(), extensions=ext
            )
        case "before_model_switch" | "after_model_switch":
            return agent(
                name="probe",
                model=model(OVERLOADED, OVERLOADED),
                fallback=[_Renamed([say("done")])],
                retry=_retry(fallback_after=2),
                extensions=ext,
            )
        case "subagent_start" | "subagent_stop":
            reviewer = agent(name="reviewer", model=model(say("fine")))
            spawn = use("spawn_agent", {"agent": "reviewer", "prompt": "Review."}, "c1")
            return agent(
                name="probe", model=model(spawn, say("done")), subagents=[reviewer], extensions=ext
            )
        case _:
            return agent(name="probe", model=model(say("done")), extensions=ext)
    return agent(
        name="probe", model=model(*TOOL_TURN), tools=tools, permissions=perms, extensions=ext
    )


def _counting(hook: HookName, calls: list[int]) -> Callable[..., Awaitable[object]]:
    async def fn(*_args: object) -> object:
        calls.append(1)
        return NOOP[hook]

    return fn


async def _events(bot: Agent[None, str], hook: HookName) -> list[Event]:
    store = sqlite(":memory:")
    if hook in ("before_compact", "after_compact"):
        # A requested compaction: the next run summarizes before its first turn request.
        first = await bot.run("hi", store=store, deps=None)
        assert isinstance(await first.thread.compact(LOCAL_OPERATOR), Ok)
        run = await bot.run("next", store=store, thread=first.thread, deps=None)
    else:
        run = await bot.run("Go.", store=store, deps=None)
    timeline = await run.thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


async def _probe(hook: HookName) -> str:
    calls: list[int] = []
    hooks: Hooks[None] = {hook: _counting(hook, calls)}  # pyright: ignore[reportAssignmentType] - probe
    if hook in ("before_compact", "after_compact"):
        bot = agent(
            name="probe",
            model=scripted_model(
                {"responses": [say("Hi."), say("The user said hi."), say("Answer.")]}
            ),
            extensions=[extension(name="probe", hooks=hooks)],
        )
    else:
        bot = _bot(hook, hooks)
    events = await _events(bot, hook)
    if not calls:
        return "unreached"
    decided = any(
        isinstance(e, HookDecisionEvent) and e.data.hook == wire_name(hook) for e in events
    )
    return "recorded" if decided else "observation"


def test_every_hooks_kind_probed_on_the_real_loop_is_hook_kinds() -> None:
    async def body() -> dict[str, str]:
        return {wire_name(h): await _probe(h) for h in NOOP}

    assert asyncio.run(body()) == dict(HOOK_KINDS)

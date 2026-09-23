"""before_model_switch gates a fallback's settings_changed; after_model_switch only observes."""

import asyncio
from dataclasses import replace

from corpus import Clock
from kit import T0, USER, Tools, acquire, allow_all, open_store
from pydantic import JsonValue

from threads.agents.context import RunContext
from threads.hooks.extension import bind, extension
from threads.hooks.types import SwitchGate
from threads.log import BranchId, ModelSettings, Principal, ThreadId
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.runtime import Idle, Runtime
from threads.loop.scripted import scripted_model
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store.lines import uuid7

OVERLOADED: JsonValue = {"error": {"reason": "overloaded", "http_status": 529}}
SMALL: JsonValue = {
    "adapter": {"name": "scripted", "settings": {}, "version": "1"},
    "model": {"name": "scripted-small", "provider": "scripted"},
    "model_params": {"max_tokens": 1024},
    "reasoning_carryover": "omit_prior",
}


def limits(name: str) -> JsonValue:
    return {
        "name": name,
        "context_window": 200000,
        "input_billing_bound": "context_window",
        "max_output_tokens": 8192,
        "price": {"input": 1000, "output": 5000},
        "provider": "scripted",
    }


RETRY: JsonValue = {
    "base_delay_ms": 1,
    "crash_resends": 2,
    "fallback_after": 1,
    "fallback_scope": "turn",
    "heartbeat_ms": 15000,
    "max_delay_ms": 10,
    "max_retries": 8,
    "max_retry_after_ms": 60000,
    "max_total_wait_ms": 600000,
}


def test_a_denied_model_switch_appends_no_settings_change() -> None:
    async def deny(_settings: ModelSettings, _ctx: RunContext[None]) -> SwitchGate:
        return {"decision": "deny", "reason": "stay on the pinned model"}

    async def main() -> list[str]:
        clock = Clock(T0)
        model = scripted_model({"responses": [OVERLOADED]})
        store = await open_store()
        thread, branch = ThreadId(uuid7(clock())), BranchId(uuid7(clock()))
        assert await store.create(thread, branch, clock()) == Ok(None)
        writer = await acquire(store, branch, "first", clock)
        tools = Tools({}, clock)
        policy: JsonValue = {
            "models": [limits("scripted-1"), limits("scripted-small")],
            "fallback": [SMALL],
            "retry": RETRY,
        }
        started: dict[str, JsonValue] = {
            "agent_name": "test",
            "config_hash": "0" * 64,
            "instructions": "Test.",
            "model": to_json(model.info.model),
            "model_params": dict(model.info.params),
            "adapter": to_json(model.info.adapter),
            "tools": [],
            "policy": policy,
        }
        principal = Principal(issuer="api", tenant="t", subject="u")
        ctx = RunContext(None, thread, branch, principal)
        hooks = bind([extension(name="ops", hooks={"before_model_switch": deny})], ctx)
        rt = Runtime(store, writer, model, tools, allow_all, clock, clock.wait_until, hooks=hooks)
        user = replace(draft("user_input", {"source": "api", "text": "go"}), actor=USER)
        assert isinstance(await rt.append(draft("thread_started", started), user), Ok)
        halt = await drive(rt)
        assert isinstance(halt, Idle)
        assert halt.reason == "model_unavailable"
        return [e.type for e in rt.events]

    appended = asyncio.run(main())
    assert "settings_changed" not in appended
    assert appended[-2:] == ["hook_decision", "turn_completed"]

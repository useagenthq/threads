"""Lane 09: `Agent.check()` returns setup failures as values and a run raises the same
ConfigError; setup is remembered per object on success only, so it is retried after a failure,
not repeated after a success, and shared by a parent and its subagent; setup makes no client, so
check() and a run can use different event loops; resolved secrets are redacted from tool results
longest first."""

import asyncio
import gc
import socket
import weakref

import pytest
from aiohttp.test_utils import TestServer
from daytona_server import API_KEY, DaytonaServer
from pydantic import BaseModel, JsonValue
from sandbox_backend import FakeBackend

from threads import (
    Completed,
    ConfigError,
    Failure,
    RunContext,
    agent,
    extension,
    scripted_model,
    secret,
    sqlite,
    tool,
)
from threads.adapters.sandboxes.daytona.sandbox import DaytonaSandbox
from threads.log import Permissions, ToolResultEvent
from threads.loop.scripted import ScriptedModel
from threads.result import Err, Ok
from threads.secrets import credential, redact_secrets, resolve

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
FAILED_THEN_SET_UP = 2
"""Setup calls when the first check fails and the second succeeds; the runs add none."""
BYPASS = Permissions.model_validate(
    {
        "mode": "bypass",
        "allow": [],
        "ask": [],
        "deny": [],
        "protected_paths": [],
        "allow_bypass": True,
        "plan_exit_mode": "default",
    }
)


def say(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "c1", "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Counted(ScriptedModel):
    """A scripted model (so the request guard lets it send) whose setup is counted and fails
    while `failing` is set."""

    def __init__(self, responses: list[JsonValue], *, failing: bool = False) -> None:
        played = scripted_model({"responses": responses})
        super().__init__(played._entries, {})
        self.failing = failing
        self.setups = 0

    async def setup(self) -> None:
        self.setups += 1
        if self.failing:
            raise ConfigError("missing_secret", "fake: set api_key or FAKE_KEY")


def test_check_returns_a_failure_and_the_run_raises_it() -> None:
    bot = agent(model=Counted([], failing=True))

    async def main() -> None:
        assert await bot.check() == Err(Failure("missing_secret", "fake: set api_key or FAKE_KEY"))
        with pytest.raises(ConfigError) as raised:
            await bot.run("hi", store=sqlite(":memory:"))
        assert raised.value.code == "missing_secret"

    asyncio.run(main())


def test_a_failed_setup_is_retried_and_a_successful_one_is_not_repeated() -> None:
    model = Counted([say("one"), say("two")], failing=True)
    bot = agent(model=model)

    async def main() -> None:
        assert isinstance(await bot.check(), Err)
        model.failing = False
        assert await bot.check() == Ok(None)
        store = sqlite(":memory:")
        assert isinstance(await bot.run("a", store=store), Completed)
        assert isinstance(await bot.run("b", store=store), Completed)

    asyncio.run(main())
    assert model.setups == FAILED_THEN_SET_UP


def test_a_model_shared_by_a_parent_and_its_subagent_is_set_up_once() -> None:
    model = Counted([])
    child = agent(name="child", model=model)
    parent = agent(name="parent", model=model, subagents=[child])
    assert asyncio.run(parent.check()) == Ok(None)
    assert asyncio.run(child.check()) == Ok(None)
    assert model.setups == 1


def test_a_parent_check_fails_on_its_subagents_setup() -> None:
    child = agent(name="child", model=Counted([], failing=True))
    parent = agent(name="parent", model=Counted([]), subagents=[child])
    checked = asyncio.run(parent.check())
    assert isinstance(checked, Err)
    assert checked.error.code == "missing_secret"


def test_an_extension_setup_failure_is_invalid_config() -> None:
    async def broken() -> None:
        raise RuntimeError("no creds")

    bot = agent(
        model=scripted_model({"responses": []}),
        extensions=[extension(name="boot", setup=broken)],
    )
    checked = asyncio.run(bot.check())
    assert isinstance(checked, Err)
    assert checked.error.code == "invalid_config"
    assert "extension boot: setup failed" in checked.error.message


def test_check_is_a_result_not_none() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": []}))
        nothing: None = await bot.check()  # pyright: ignore[reportAssignmentType] - a result
        assert nothing == Ok(None)

    asyncio.run(main())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_check_and_a_run_on_different_event_loops() -> None:
    """Nothing check() makes survives its loop: the run opens its own HTTP session, on its own
    loop, and every Daytona call of the run goes through it."""
    port = _free_port()
    sandbox = DaytonaSandbox(API_KEY, api_url=f"http://127.0.0.1:{port}", poll_s=0.0, wait_s=5.0)
    model = scripted_model({"responses": [use("bash", {"command": "echo hi"}), say("Done.")]})
    bot = agent(model=model, sandbox=sandbox, permissions=BYPASS)
    assert asyncio.run(bot.check()) == Ok(None)
    backend = FakeBackend.scripted()

    async def run() -> None:
        server = DaytonaServer(backend)
        async with TestServer(server.app, host="127.0.0.1", port=port) as test:
            server.base = str(test.make_url("")).rstrip("/")
            try:
                result = await bot.run("go", store=sqlite(":memory:"))
            finally:
                await sandbox.aclose()
        assert isinstance(result, Completed)

    asyncio.run(run())
    assert backend.boxes
    assert backend.argvs


def test_a_longer_value_is_replaced_whole_even_when_a_prefix_came_first() -> None:
    credential("short", "api_key", "abc-lane09", "UNUSED")
    credential("long", "api_key", "abc-lane09-123", "UNUSED")
    assert redact_secrets("x abc-lane09-123 y abc-lane09") == (
        "x [secret long.api_key] y [secret short.api_key]"
    )


def test_one_value_under_two_labels_redacts_to_the_smaller_label() -> None:
    credential("zeta", "api_key", "same-lane09-value", "UNUSED")
    credential("alpha", "api_key", "same-lane09-value", "UNUSED")
    assert redact_secrets("same-lane09-value") == "[secret alpha.api_key]"


def test_missing_or_empty_names_the_option_and_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("THREADS_TEST_CRED_UNSET", raising=False)
    for value in (None, ""):
        with pytest.raises(ConfigError) as raised:
            credential("fake", "api_key", value, "THREADS_TEST_CRED_UNSET")
        assert raised.value.message == "fake: set api_key or THREADS_TEST_CRED_UNSET"
    with pytest.raises(ConfigError) as named:
        credential("fake", "api_key", secret("THREADS_TEST_CRED_UNSET"), "OTHER")
    assert named.value.message == "fake: set api_key or THREADS_TEST_CRED_UNSET"


class NoInput(BaseModel):
    pass


def test_a_resolved_secret_is_recorded_redacted_in_a_tool_result() -> None:
    value = resolve(secret("THREADS_TEST_TOKEN"), {"THREADS_TEST_TOKEN": "tok-lane09-5e2d"})

    async def leak(_args: NoInput, _ctx: RunContext[None]) -> str:
        return f"token={value}"

    leaky = tool(name="leak", description="Leak.", input=NoInput, execute=leak)
    model = scripted_model({"responses": [use("leak", {}), say("ok")]})
    bot = agent(model=model, tools=[leaky], permissions=BYPASS)

    async def main() -> str:
        result = await bot.run("go", store=sqlite(":memory:"), deps=None)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        (done,) = [e.event for e in timeline.value.entries if isinstance(e.event, ToolResultEvent)]
        return done.data.preview

    assert asyncio.run(main()) == "token=[secret THREADS_TEST_TOKEN]"


def test_a_set_up_adapter_is_not_kept_alive_by_the_setup_memory() -> None:
    model = Counted([])
    alive = weakref.ref(model)
    assert asyncio.run(agent(model=model).check()) == Ok(None)
    del model
    gc.collect()
    assert alive() is None

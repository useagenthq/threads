"""the agent options end to end: gated tools pinned only with their capability, setup
errors naming a missing one, the permission class of web tools, and a web_fetch run whose
result is text plus a web citation naming the fetched artifact."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
from local_sandbox import LOCAL_INFO, LocalSandbox
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import Completed, ConfigError, agent, scripted_model, sqlite
from threads.agents import catalog
from threads.agents.bindings import category
from threads.agents.catalog import GitOptions, LspOptions, WebOptions
from threads.log import CitationPart, Event, Permissions, ThreadStartedEvent, ToolResultEvent
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.search import exa
from threads.secrets import secret
from threads.web.guard import Target
from threads.web.http import Fence, Request, Response, WebError
from threads.web.tools import WebTools

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
DONE: JsonValue = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}
DESKTOP = replace(LOCAL_INFO, desktop="image")


class _Options(TypedDict, total=False):
    sandbox: Sandbox
    web: WebOptions
    git: GitOptions
    computer: bool
    lsp: LspOptions


def _pinned(**options: Unpack[_Options]) -> set[str]:
    bot = agent(model=scripted_model({"responses": [DONE]}), **options)
    return {s.name for s in bot.definition.specs()}


def test_gated_tools_are_pinned_only_with_their_capability(tmp_path: Path) -> None:
    box = LocalSandbox(tmp_path, DESKTOP)
    base = _pinned(sandbox=box)
    assert "notebook_edit" in base
    assert not base & {"web_fetch", "web_search", "computer", "lsp", "git_push"}
    full = _pinned(
        sandbox=box,
        web={"fetch": True, "search": exa(secret("EXA_KEY"))},
        git={"credential": secret("GH_TOKEN")},
        computer=True,
        lsp={"languages": ["python", "go"]},
    )
    assert full - base == {
        "web_fetch",
        "web_search",
        "git_clone",
        "git_fetch",
        "git_push",
        "open_pull_request",
        "computer",
        "computer_screenshot",
        "lsp",
    }
    assert _pinned(web={"fetch": True}) - _pinned() == {"web_fetch"}


def test_a_missing_capability_is_a_setup_error(tmp_path: Path) -> None:
    plain, desktop = LocalSandbox(tmp_path), LocalSandbox(tmp_path, DESKTOP)
    gh = secret("GH_TOKEN")
    for code, make in (
        ("capability_missing", lambda: _pinned(computer=True)),
        ("capability_missing", lambda: _pinned(computer=True, sandbox=plain)),
        ("unknown_preset", lambda: _pinned(lsp={"languages": ["cobol"]}, sandbox=desktop)),
        ("capability_missing", lambda: _pinned(lsp={"languages": ["python"]})),
        ("capability_missing", lambda: _pinned(git={"credential": gh})),
    ):
        with pytest.raises(ConfigError) as raised:
            make()
        assert raised.value.code == code


def test_web_tools_count_as_other_for_permissions() -> None:
    specs = {
        s.name: s
        for s in agent(
            model=scripted_model({"responses": []}), web={"fetch": True}
        ).definition.specs()
    }
    assert specs["web_fetch"].effect_class == "read_only"
    assert category(specs["web_fetch"]) == "other"
    assert category(specs["read_tool_result"]) == "read_only"


class _Page:
    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        if not await fence():
            return Err(WebError("stale_epoch", "lost", sent=False))
        body = b"<h1>Docs</h1><p>Hello.</p>"
        return Ok(Response(200, {"content-type": "text/html"}, body))


async def _public(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def test_a_web_fetch_result_is_text_and_a_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(fence: Fence, put: object, search: object = None) -> WebTools:
        return WebTools(fence, put, None, _Page(), _public)  # type: ignore[arg-type] - put is the run's

    monkeypatch.setattr(catalog, "WebTools", offline)
    use: JsonValue = {
        "content": [
            {
                "type": "tool_use",
                "call_id": "call_1",
                "name": "web_fetch",
                "input": {"url": "https://docs.example.com/start"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": USAGE,
    }
    allow = Permissions.model_validate(
        {
            "mode": "default",
            "allow": ["web_fetch(domain:docs.example.com)"],
            "ask": [],
            "deny": [],
            "protected_paths": [],
            "allow_bypass": False,
            "plan_exit_mode": "default",
        }
    )

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(
            model=scripted_model({"responses": [use, DONE]}), web={"fetch": True}, permissions=allow
        )
        result = await bot.run("read the docs", store=store)
        assert isinstance(result, Completed)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        logged: list[Event] = [e.event for e in timeline.value.entries]
        started = next(e for e in logged if isinstance(e, ThreadStartedEvent))
        assert "web_fetch" in {t.name for t in started.data.tools}
        done = next(e for e in logged if isinstance(e, ToolResultEvent))
        assert not done.data.is_error
        assert done.data.preview.endswith("# Docs\n\nHello.")
        content = done.data.content
        assert content is not MISSING
        assert [p.type for p in content] == ["text", "citation"]
        cite = content[1]
        assert isinstance(cite, CitationPart)
        assert cite.source_id == "https://docs.example.com/start"

    asyncio.run(main())


def test_git_takes_a_forge_url_and_an_api_url_for_an_enterprise_host(tmp_path: Path) -> None:
    configured = catalog.catalog(
        LocalSandbox(tmp_path, LOCAL_INFO),
        web=None,
        git={
            "credential": secret("GH_TOKEN"),
            "forge_url": "https://git.acme.dev/",
            "api_url": "https://git.acme.dev/api/v3",
        },
        computer=False,
        lsp=None,
    )
    assert configured.forge.git_url("acme/api") == "https://git.acme.dev/acme/api.git"
    assert configured.forge.api.api == "https://git.acme.dev/api/v3"
    default = catalog.catalog(
        LocalSandbox(tmp_path, LOCAL_INFO),
        web=None,
        git={"credential": secret("GH_TOKEN")},
        computer=False,
        lsp=None,
    )
    assert default.forge.git_url("acme/api") == "https://github.com/acme/api.git"
    assert default.forge.api.api == "https://api.github.com"

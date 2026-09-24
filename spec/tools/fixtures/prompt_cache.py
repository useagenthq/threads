# pyright: strict
"""Prompt caching is a line-0 setting (lane 10; spec/schema/README.md, Cache controls): changing
it is a new settings epoch, never a history change."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, MODEL, PARAMS, tokens
from .log import Log
from .pieces import READ_FILE, render_case, started, user
from .policies import policy

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

FAM = "cancellation_resume"


def build(root: pathlib.Path) -> None:
    _prompt_cache_epochs(root)


def _prompt_cache_epochs(root: pathlib.Path) -> None:
    def adapter(ttl: str) -> Obj:
        return {"name": "anthropic", "version": "1", "settings": {"prompt_cache": ttl}}

    log = Log()
    started(log, [READ_FILE], policy=policy(), adapter=adapter("5m"))
    user(log, "hi")
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": "Hello!"}], "end_turn", tokens(40, 3))
    log.add("turn_completed", {"reason": "end_turn"})
    settings: Obj = {
        "model": MODEL,
        "model_params": PARAMS,
        "adapter": adapter("1h"),
        "reasoning_carryover": "keep",
    }
    log.add(
        "settings_changed", {"reason": "user", "settings": settings}, actor="user", principal=ALICE
    )
    user(log, "again")
    render_case(
        root,
        (
            "render-adapter-settings-prompt-cache",
            FAM,
            "Adapter setting prompt_cache is line 0: the thread started with 5m, and a "
            "settings_changed to 1h starts a new settings epoch whose line 0 carries 1h. Each "
            "epoch's line 0 is byte-identical in both runners, and history bytes don't change.",
        ),
        log,
    )

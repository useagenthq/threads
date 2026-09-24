# pyright: strict
"""Calls whose tool is gone: made to a tool not in the set, or to one a later tools_changed
removed. Recovery closes a never-begun one as not_executed (spec/schema/README.md, "A call whose
tool is gone never runs")."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, tokens
from .log import Log, reduce
from .pieces import (
    ALLOW,
    EMAIL,
    EMAIL_IN,
    FINAL,
    READ_FILE,
    TAIL,
    case,
    effect_call,
    started,
    user,
    write_case,
)
from .policies import permissions, policy
from .tool_sets import DEPLOY, changed

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

FAM = "cancellation_resume"
DEPLOY_IN: Obj = {"env": "prod"}


def _unknown_call(log: Log, name: str, inp: Obj) -> None:
    """A tool_call to `name` with no result: the live loop would close it pre-effect."""
    r = log.model_request()
    use: Obj = {"type": "tool_use", "call_id": "call_1", "name": name, "input": inp}
    log.model_response(r, [use], "tool_use", tokens(80, 20))
    log.tool_call(r, "call_1", name, inp)


def _closed(root: pathlib.Path, meta: tuple[str, str], log: Log, preview: str, tool: str) -> None:
    write_case(
        root,
        case(
            meta[0],
            FAM,
            "recover",
            meta[1],
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "tool_result",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_1",
                        "is_error": True,
                        "origin": "not_executed",
                        "preview": preview,
                    },
                },
                *TAIL,
            ],
            "sandbox": {"dispatches": {tool: 0}},
        },
        extra={
            "model.json": {"responses": [FINAL]},
            "sandbox.json": {"tools": {tool: {"output": "ran"}}},
        },
    )


def _unknown(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE])
    user(log, "Deploy to prod.")
    _unknown_call(log, "mcp__ops__deploy", DEPLOY_IN)
    log.add("permission_decision", {"call_id": "call_1", **ALLOW})
    changed(log, [READ_FILE, DEPLOY])
    _closed(
        root,
        (
            "recover-unknown-tool-call-not-executed",
            "A call to mcp__ops__deploy, which was not in the tool set when the call was made, "
            "then a valid tools_changed adds it. The call never had a tool, so recovery closes "
            "it not_executed and never dispatches it under the later spec.",
        ),
        log,
        "not executed: unknown tool mcp__ops__deploy",
        "mcp__ops__deploy",
    )


def _removed(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE, EMAIL], policy=policy(permissions=permissions("plan")))
    user(log, "Read a.")
    _unknown_call(log, "read_file", {"path": "a"})
    changed(log, [EMAIL])
    _closed(
        root,
        (
            "recover-removed-tool-call-not-executed",
            "Plan mode. A read_file call is recorded, not yet authorized, and a valid "
            "tools_changed then removes read_file. A removal is a policy change: recovery closes "
            "the never-begun call not_executed instead of authorizing or dispatching it.",
        ),
        log,
        "not executed: read_file was removed from the tool set",
        "read_file",
    )
    log = Log()
    started(log, [READ_FILE, EMAIL])
    effect_call(log, EMAIL, EMAIL_IN, "Email bob that the build is green.")
    changed(log, [READ_FILE])
    _closed(
        root,
        (
            "recover-removed-tool-allowed-call-not-dispatched",
            "An allowed send_email call that never began, then a valid tools_changed removes "
            "send_email. Recovery closes it not_executed: an allow recorded before the removal "
            "never dispatches a call past it.",
        ),
        log,
        "not executed: send_email was removed from the tool set",
        "send_email",
    )


def build(root: pathlib.Path) -> None:
    _unknown(root)
    _removed(root)

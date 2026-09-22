# pyright: strict
"""Mid-thread changes: tool-set changes (tools_changed) and recovered model responses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, sha, tokens, tool
from .jcs import JsonValue, canonical
from .log import Log, reduce
from .pieces import FINAL, READ_FILE, case, negative, started, user, write_case
from .render import render

if TYPE_CHECKING:
    import pathlib

MCP_SEARCH = tool(
    "mcp__docs__search",
    "Search the docs server.",
    {"q": {"type": "string"}},
    "unguarded",
)


def tools_changed(log: Log, tools: list[JsonValue], tools_hash: str | None = None) -> None:
    log.add(
        "tools_changed",
        {"tools": tools, "tools_hash": tools_hash or sha(canonical(tools))},
    )


def build(root: pathlib.Path) -> None:
    # tools-changed-mid-thread
    log = Log()
    started(log, [READ_FILE])
    user(log, "hi")
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": "Hello!"}], "end_turn", tokens(40, 3))
    log.add("turn_completed", {"reason": "end_turn"})
    tools_changed(log, [READ_FILE, MCP_SEARCH])
    user(log, "Search the docs for fork.")
    body, line0 = render(log.events, log.artifacts)
    write_case(
        root,
        case(
            "tools-changed-mid-thread",
            "tools_streaming",
            "render",
            "An MCP server adds a tool mid-thread. tools_changed records the full new set with "
            "its hash and "
            "renders as a line after the declared prefix: the log holds exactly what the model "
            "saw (C6) and "
            "line 0 stays byte-equal (C7).",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "render": {
                "next_request_sha256": sha(body),
                "declared_prefix": {"bytes": len(line0), "sha256": sha(line0)},
            },
        },
        extra={"request.bytes": body},
    )

    # tools-changed-hash-mismatch
    log = Log()
    started(log, [READ_FILE])
    tools_changed(log, [READ_FILE, MCP_SEARCH], tools_hash=sha(canonical([READ_FILE])))
    negative(
        root,
        "tools-changed-hash-mismatch",
        "tools_changed.tools_hash is not the hash of the RFC 8785 bytes of its tools: "
        "invalid_transition.",
        log,
        ("invalid_transition", log.seq),
    )

    # model-response-recovered-by-lookup
    log = Log()
    started(log, [READ_FILE])
    user(log, "Say done.")
    r = log.model_request()
    content = FINAL["content"]
    write_case(
        root,
        case(
            "model-response-recovered-by-lookup",
            "cancellation_resume",
            "recover",
            "Crash after a model_request was dispatched, before its response was recorded. The "
            "adapter supports "
            "lookup and finds the response with finality, so recovery records "
            "model_response_recovered and "
            "makes no new model call. A non-final not_found would instead abandon the attempt "
            "as unknown.",
            model_script="model.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "model_response_recovered",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "request_event_id": r["event_id"],
                        "provider_request_id": "msg_01",
                        "content": content,
                        "stop_reason": "end_turn",
                        "completeness": "complete",
                    },
                },
                {"type": "turn_completed", "data": {"reason": "end_turn"}},
            ],
        },
        extra={
            "model.json": {
                "responses": [],
                "lookup": {
                    str(r["event_id"]): {
                        "result": "found",
                        "final": True,
                        "provider_request_id": "msg_01",
                        "response": FINAL,
                    }
                },
            }
        },
    )

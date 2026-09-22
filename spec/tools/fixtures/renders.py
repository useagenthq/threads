# pyright: strict
"""Render v1 and C7 cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, ALLOW, NOW, sha, tokens
from .log import Log, reduce
from .pieces import READ_FILE, TESTS, case, started, user, write_case
from .render import render

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    # render: C7 declared prefix equality
    def two_turns() -> Log:
        log = Log()
        started(log, [READ_FILE], "You answer briefly.")
        user(log, "hi")
        r = log.model_request()
        log.model_response(r, [{"type": "text", "text": "Hello!"}], "end_turn", tokens(40, 3))
        log.add("turn_completed", {"reason": "end_turn"})
        user(log, "What do you remember about me?")
        log.add(
            "injected",
            {
                "source": "memory",
                "trust": "untrusted_reference",
                "origin": {"id": "mem_7", "version": "1"},
                "text": "User prefers short answers.",
            },
        )
        return log

    log = two_turns()
    r2 = log.model_request()
    log.model_response(
        r2, [{"type": "text", "text": "You like short answers."}], "end_turn", tokens(60, 5)
    )
    log.add("turn_completed", {"reason": "end_turn"})
    user(log, "Thanks.")
    body, line0 = render(log.events, log.artifacts)
    write_case(
        root,
        case(
            "prefix-stable-across-turns",
            "context_compaction",
            "render",
            "C7: the declared prefix (Render v1 line 0: model, params, adapter, system, tools) "
            "is byte-EQUAL on every recorded request and on the next one. Every recorded "
            "request re-renders to its req_hash, and the next request equals request.bytes. "
            "Recalled memory renders after the prefix inside the untrusted reference wrapper.",
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

    log = two_turns()
    body, line0 = render(log.events, log.artifacts)
    grown = line0 + body[len(line0) :].split(b"\n", 1)[0] + b"\n"
    e = log.add(
        "model_request",
        {
            "attempt": 1,
            "request_ref": log.art(body, "application/x-ndjson"),
            "declared_prefix": {"bytes": len(grown), "sha256": sha(grown)},
        },
    )
    write_case(
        root,
        case(
            "prefix-declared-changed-fails",
            "context_compaction",
            "render",
            "Negative C7 case: the second request declares a longer prefix whose first bytes "
            "are exactly the old declared prefix. A starts-with check would pass; C7 requires "
            "equality, so it fails with prefix_changed.",
        ),
        log,
        {"outcome": "error", "error": {"code": "prefix_changed", "seq": e["seq"]}},
    )

    log = two_turns()
    body, _ = render(log.events, log.artifacts)
    wrong = body.replace(b"User prefers short answers.", b"User prefers long answers.")
    _, line0 = render(log.events, log.artifacts)
    e = log.add(
        "model_request",
        {
            "attempt": 1,
            "request_ref": log.art(wrong, "application/x-ndjson"),
            "declared_prefix": {"bytes": len(line0), "sha256": sha(line0)},
        },
    )
    write_case(
        root,
        case(
            "render-req-hash-mismatch",
            "log_fork_test",
            "render",
            "The recorded request artifact is intact (its sha256 matches) but is not what "
            "Render v1 produces from the events before it: request_hash_mismatch.",
        ),
        log,
        {
            "outcome": "error",
            "error": {"code": "request_hash_mismatch", "seq": e["seq"]},
        },
    )

    # render: reserved control events and non-blocking results
    log = Log()
    started(log, [TESTS], "You run tests.")
    user(log, "Run the tests.")
    r = log.model_request()
    log.model_response(
        r,
        [{"type": "tool_use", "call_id": "call_1", "name": "run_tests", "input": {}}],
        "tool_use",
        tokens(30, 5),
    )
    log.tool_call(r, "call_1", "run_tests", {})
    log.add("permission_decision", {"call_id": "call_1", **ALLOW})
    log.add(
        "tool_result",
        {
            "call_id": "call_1",
            "is_error": False,
            "completeness": "complete",
            "preview": "running",
            "origin": "deferred",
        },
        actor="tool",
    )
    r = log.model_request()
    log.model_response(
        r, [{"type": "text", "text": "Tests are running."}], "end_turn", tokens(40, 4)
    )
    log.add("turn_completed", {"reason": "end_turn"})
    log.add("heartbeat", {"running_call_ids": ["call_1"]})
    user(log, "Any results?")
    log.add(
        "steer",
        {"source": "api", "text": "Also check lint."},
        actor="user",
        principal=ALICE,
    )
    log.add(
        "tool_result_late",
        {
            "call_id": "call_1",
            "is_error": False,
            "completeness": "complete",
            "preview": "12 passed",
        },
        actor="tool",
    )
    body, line0 = render(log.events, log.artifacts)
    write_case(
        root,
        case(
            "render-reserved-control-events",
            "tools_streaming",
            "render",
            "A non-blocking call gets a deferred placeholder result; later a heartbeat, a new "
            "turn, a steer and the late result arrive. Each renders as its own appended line "
            "and nothing earlier is rewritten, so the declared prefix stays equal.",
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

# pyright: strict
"""Context ladder cases: max-output, breaker, restore, summarizer fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, num, sha, tokens
from .log import Log, reduce
from .pieces import (
    FINAL,
    READ_FILE,
    answer,
    call,
    case,
    read_turn,
    reduce_case,
    render_case,
    result,
    started,
    user,
    write_case,
)
from .policies import CONTEXT, RETRY, policy
from .projections import compaction

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj
    from .pieces import Meta

FAM = "context_compaction"
CONTINUE = "Output limit reached. Continue exactly where you stopped. Do not repeat earlier output."
SUMMARY = "The user asked what README.md holds (one heading, demo)."


def build(root: pathlib.Path) -> None:
    _max_output(root)
    _breaker(root)
    _restore(root)
    _fallback(root)


def _crashed(log: Log, question: str) -> Obj:
    user(log, question)
    r = log.model_request()
    return {
        "type": "model_attempt_abandoned",
        "actor_kind": "recovery",
        "epoch": 2,
        "data": {
            "request_event_id": r["event_id"],
            "provider_outcome": "unknown",
            "reason": "crash",
        },
    }


def _recover(
    root: pathlib.Path, meta: Meta, log: Log, steps: tuple[list[JsonValue], list[JsonValue]]
) -> None:
    appended, responses = steps
    write_case(
        root,
        case(meta[0], FAM, "recover", meta[2], model_script="model.json"),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "appended": appended},
        extra={"model.json": {"responses": responses}},
    )


def _max_output(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT, retry=RETRY))
    crash = _crashed(log, "Write the long report.")
    cut: Obj = {
        "content": [{"type": "text", "text": "Part"}],
        "stop_reason": "max_tokens",
        "usage": tokens(50, 1024),
    }
    more: Obj = {"source": "recovery", "trust": "trusted_instruction", "text": CONTINUE}
    appended: list[JsonValue] = [crash, {"type": "model_request", "data": {"attempt": 2}}]
    for i in range(4):
        appended.append({"type": "model_response", "data": {"stop_reason": "max_tokens"}})
        if i < 3:  # noqa: PLR2004 - max_output_continuations is 3
            appended.append({"type": "injected", "data": more})
            appended.append({"type": "model_request", "data": {"attempt": 1}})
    appended.append({"type": "turn_completed", "data": {"reason": "max_output"}})
    _recover(
        root,
        (
            "max-output-continuation-bounded",
            FAM,
            "Every response stops at max_tokens. With no escalation configured, the loop "
            "appends the fixed trusted continuation instruction and asks again, three times "
            "(max_output_continuations), then ends the turn max_output. A tool_use cut by "
            "max_tokens never exists, so nothing is dispatched.",
        ),
        log,
        (appended, [cut, cut, cut, cut]),
    )


def _breaker(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT))
    read_turn(log)
    for _ in range(3):
        side = log.model_request(compaction=True)
        log.add(
            "model_attempt_abandoned",
            {
                "request_event_id": side["event_id"],
                "provider_outcome": "failed",
                "reason": "server_error",
                "http_status": 500,
            },
        )
        log.add(
            "compaction_failed",
            {"stage": "summary", "reason": "model_error", "request_event_id": side["event_id"]},
        )
    user(log, "Next question.")
    reduce_case(
        root,
        (
            "compaction-breaker-opens",
            FAM,
            "Three consecutive compaction_failed since the last compacted reach "
            "compact.max_failures: the breaker is open, derived from the log with no breaker "
            "event. Automatic compaction stops; clearing and manual compaction still run.",
        ),
        log,
        {"compaction": compaction(log)},
    )


def _compact(log: Log, first: Obj, last: Obj, trigger: str) -> None:
    side_id = next(e for e in reversed(log.events) if e["type"] == "model_request")["event_id"]
    log.add(
        "compacted",
        {
            "from_seq": first["seq"],
            "to_seq": last["seq"],
            "from_event_id": first["event_id"],
            "to_event_id": last["event_id"],
            "summary_ref": log.art(SUMMARY.encode(), "text/plain"),
            "summary_request_event_id": side_id,
            "trigger": trigger,
        },
    )


def _restore(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT))
    user(log, "Use the deploy skill and fix src/app.ts.")
    first = log.events[-1]
    skill: Obj = {
        "source": "skill",
        "trust": "trusted_instruction",
        "origin": {"id": "deploy", "version": "3"},
        "text": "Deploy with make deploy.",
    }
    log.add("injected", skill)
    call(log, "read_file", {"path": "src/app.ts"})
    result(log, "call_1", "export const x = 1;\n")
    answer(log, "Read it.")
    last = log.events[-1]
    side = log.model_request(compaction=True)
    log.model_response(side, [{"type": "text", "text": SUMMARY}], "end_turn", tokens(900, 40))
    _compact(log, first, last, "threshold")
    log.add("injected", skill)
    body = "export const x = 1;\n"
    attach: Obj = {
        "source": "attachment",
        "trust": "untrusted_reference",
        "origin": {"id": "src/app.ts", "version": sha(body.encode())},
        "text": body,
    }
    log.add("injected", attach)
    todo: Obj = {
        "source": "todo",
        "trust": "untrusted_reference",
        "origin": {"id": "todos"},
        "text": "- [in_progress] Fix src/app.ts",
    }
    log.add("injected", todo)
    log.add("heartbeat", {"running_call_ids": ["call_bg"]})
    user(log, "Continue.")
    render_case(
        root,
        (
            "compaction-restores-active-context",
            FAM,
            "After compacted, the restore events follow in the fixed order: skills from the "
            "dropped range (same origin), recently read files as untrusted attachments with "
            "their content hash, the todo list, then a heartbeat of running work. The next "
            "request renders summary, restores, then the new input.",
        ),
        log,
    )


def _fallback(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT, retry=RETRY))
    read_turn(log)
    turn_end = log.events[-1]
    crash = _crashed(log, "Now summarize README.md.")
    too_long: Obj = {"error": {"reason": "prompt_too_long", "http_status": 400}}
    summary: Obj = {
        "content": [{"type": "text", "text": SUMMARY}],
        "stop_reason": "end_turn",
        "usage": tokens(700, 30),
    }
    ptl: Obj = {"provider_outcome": "failed", "reason": "prompt_too_long", "http_status": 400}
    clear: Obj = {
        "reason": "compaction_fallback",
        "edits": [{"call_id": "call_1", "action": "clear"}],
    }
    appended: list[JsonValue] = [
        crash,
        {"type": "model_request", "data": {"attempt": 2}},
        {"type": "model_attempt_abandoned", "data": ptl},
        {"type": "model_request", "data": {"purpose": "compaction", "attempt": 1}},
        {"type": "model_attempt_abandoned", "data": ptl},
        {"type": "context_edited", "data": clear},
        {"type": "model_request", "data": {"purpose": "compaction", "attempt": 2}},
        {"type": "model_response", "data": {"content": summary["content"]}},
        {
            "type": "compacted",
            "data": {"from_seq": 2, "to_seq": num(turn_end["seq"]), "trigger": "reactive"},
        },
        {"type": "model_request", "data": {"attempt": 3}},
        {"type": "model_response", "data": {"content": FINAL["content"]}},
        {"type": "turn_completed", "data": {"reason": "end_turn"}},
    ]
    _recover(
        root,
        (
            "compaction-summarizer-fails-falls-back",
            FAM,
            "The turn request is too long, and so is the summarizer side request. The fallback "
            "clears every clearable result (context_edited{compaction_fallback}, keep 0) and "
            "retries the side request once; it succeeds, compaction completes and the turn "
            "request is re-sent.",
        ),
        log,
        (appended, [too_long, too_long, summary, FINAL]),
    )

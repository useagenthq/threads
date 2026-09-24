# pyright: strict
"""OpenTelemetry goldens of plain turns: model and tool spans, usage, content, retries,
compaction, clock skew and a long turn sent over several syncs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import BRANCH, T0, num, tokens
from .log import Log
from .otel_case import TENANT, Sync, write
from .otel_expect import Ctx, Span, event
from .otel_shapes import chat_of, tool_of, turn_of
from .pieces import READ_FILE, answer, call, read_turn, result, started, user
from .policies import CONTEXT, policy

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

CTX = Ctx(TENANT, BRANCH)
SUMMARY = "The user asked what README.md holds: one heading, demo."


def build(root: pathlib.Path) -> None:
    _model_tool(root, "otel-turn-model-tool", content=False)
    _model_tool(root, "otel-content-on", content=True)
    _cached(root)
    _null_usage(root)
    _retry(root)
    _retry_between(root)
    _retry_after_compaction(root)
    _compaction(root)
    _clock_skew(root)
    _long_turn(root)


def read_spans(ctx: Ctx, log: Log, first: int) -> list[Span]:
    """The four spans of `read_turn` written from seq `first` (its user_input)."""
    e = log.events
    t = turn_of(ctx, e[first - 1], e[first + 7])
    return [
        t,
        chat_of(ctx, t, e[first], e[first + 1]),
        tool_of(ctx, t, log, first + 3, first + 5, "read_only"),
        chat_of(ctx, t, e[first + 5], e[first + 6]),
    ]


def _model_tool(root: pathlib.Path, name: str, content: bool) -> None:
    log = Log()
    started(log, [READ_FILE])
    read_turn(log)
    ctx = Ctx(TENANT, BRANCH, content=content)
    desc = (
        "One turn with two model calls and a read-only tool: a turn span, two chat spans and "
        "an execute_tool span carrying the permission decision, all in the user_input's trace."
    )
    if content:
        desc = (
            "The same turn with content: true: the tool span carries the canonical arguments "
            "and the result preview, and each chat span the response text."
        )
    spans = read_spans(ctx, log, 2)
    write(root, name, desc, [log], [Sync(BRANCH, log.seq, 0, spans)], content=content)


def _one_answer(log: Log, usage: Obj) -> list[Span]:
    u = user(log, "Hi.")
    r = log.model_request()
    resp = log.model_response(r, [{"type": "text", "text": "Hello."}], "end_turn", usage)
    end = log.add("turn_completed", {"reason": "end_turn"})
    t = turn_of(CTX, u, end)
    return [t, chat_of(CTX, t, r, resp)]


def _cached(root: pathlib.Path) -> None:
    log = Log()
    started(log, [])
    full: Obj = {**tokens(100, 20), "cache_read_tokens": 300, "cache_write_tokens": 50}
    spans = _one_answer(log, {**full, "reasoning_tokens": 5})
    spans += _one_answer(
        log, {**tokens(10, 2), "cache_read_tokens": 30, "cache_write_tokens": None}
    )
    write(
        root,
        "otel-cached-usage",
        "gen_ai.usage.input_tokens counts cached input too: 100 + 300 read + 50 written = 450, "
        "and the cache attributes are also sent on their own. In the second turn the cache "
        "write is null, so input_tokens is left out while every known part is sent.",
        [log],
        [Sync(BRANCH, log.seq, 0, spans)],
    )


def _null_usage(root: pathlib.Path) -> None:
    log = Log()
    started(log, [])
    spans = _one_answer(log, {"input_tokens": None, "output_tokens": None})
    write(
        root,
        "otel-null-usage",
        "Usage that never arrived is null: no usage attribute is sent, never a 0.",
        [log],
        [Sync(BRANCH, log.seq, 0, spans)],
    )


def _abandon(log: Log, r: Obj, reason: str, status: int) -> Obj:
    data: Obj = {
        "request_event_id": r["event_id"],
        "provider_outcome": "failed",
        "reason": reason,
        "http_status": status,
    }
    return log.add("model_attempt_abandoned", data)


def _retry_log() -> tuple[Log, list[Obj]]:
    """user, a request abandoned as overloaded, retry_scheduled, the retried attempt, the end."""
    log = Log()
    started(log, [])
    u = user(log, "Say done.")
    r1 = log.model_request()
    lost = _abandon(log, r1, "overloaded", 529)
    wait: Obj = {
        "request_event_id": r1["event_id"],
        "delay_ms": 2000,
        "not_before": num(lost["time"]) + 2000,
        "basis": "backoff",
    }
    retry = log.add("retry_scheduled", wait)
    r2 = log.model_request(attempt=2)
    resp = log.model_response(r2, [{"type": "text", "text": "Done."}], "end_turn", tokens(50, 2))
    end = log.add("turn_completed", {"reason": "end_turn"})
    return log, [u, r1, lost, retry, r2, resp, end]


def _retry_spans(e: list[Obj]) -> tuple[Span, Span, Span]:
    u, r1, lost, retry, r2, resp, end = e
    t = turn_of(CTX, u, end)
    failed = chat_of(CTX, t, r1, lost)
    retried = chat_of(CTX, t, r2, resp, (event(retry, failed.id),))
    return t, failed, retried


def _retry(root: pathlib.Path) -> None:
    log, e = _retry_log()
    write(
        root,
        "otel-abandoned-retry",
        "An abandoned attempt is an ERROR chat span with error.type overloaded. The "
        "retry_scheduled is an event on the retried attempt's span, naming the failed span.",
        [log],
        [Sync(BRANCH, log.seq, 0, list(_retry_spans(e)))],
    )


def _retry_between(root: pathlib.Path) -> None:
    log, e = _retry_log()
    t, failed, retried = _retry_spans(e)
    at = num(e[2]["seq"])
    write(
        root,
        "otel-retry-tick-between",
        "A sync runs right after the abandonment, before retry_scheduled: the failed span is "
        "sent without the retry, which arrives with the retried attempt. Together the two "
        "syncs send exactly what one sync at the end sends.",
        [log],
        [Sync(BRANCH, at, 0, [failed]), Sync(BRANCH, log.seq, at, [retried, t])],
    )


def _compacted(log: Log, side: Obj, first: Obj, last: Obj, trigger: str) -> Obj:
    data: Obj = {
        "from_seq": first["seq"],
        "to_seq": last["seq"],
        "from_event_id": first["event_id"],
        "to_event_id": last["event_id"],
        "summary_ref": log.art(SUMMARY.encode(), "text/plain"),
        "summary_request_event_id": side["event_id"],
        "trigger": trigger,
    }
    return log.add("compacted", data)


def _summarize(log: Log) -> tuple[Obj, Obj]:
    side = log.model_request(compaction=True)
    content: list[JsonValue] = [{"type": "text", "text": SUMMARY}]
    return side, log.model_response(side, content, "end_turn", tokens(900, 40))


def _retry_after_compaction(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT))
    read_turn(log)
    first, last = log.events[1], log.events[-1]
    u = user(log, "Now summarize README.md.")
    r1 = log.model_request()
    lost = _abandon(log, r1, "overloaded", 529)
    wait: Obj = {
        "request_event_id": r1["event_id"],
        "delay_ms": 1000,
        "not_before": num(lost["time"]) + 1000,
        "basis": "backoff",
    }
    retry = log.add("retry_scheduled", wait)
    side, summary = _summarize(log)
    done = _compacted(log, side, first, last, "threshold")
    r2 = log.model_request(attempt=2)
    resp = log.model_response(
        r2, [{"type": "text", "text": "One heading."}], "end_turn", tokens(80, 4)
    )
    end = log.add("turn_completed", {"reason": "end_turn"})
    t = turn_of(CTX, u, end, (event(done),))
    failed = chat_of(CTX, t, r1, lost)
    spans = [
        *read_spans(CTX, log, 2),
        t,
        failed,
        chat_of(CTX, t, side, summary),
        chat_of(CTX, t, r2, resp, (event(retry, failed.id),)),
    ]
    write(
        root,
        "otel-retry-after-compaction",
        "An abandoned attempt, then a compaction side request, then the retried attempt: the "
        "retry event goes on the retried attempt's chat span, never on the compaction's.",
        [log],
        [Sync(BRANCH, log.seq, 0, spans)],
    )


def _compaction(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT))
    read_turn(log)
    first, last = log.events[1], log.events[-1]
    u = user(log, "Now summarize README.md.")
    blocked: Obj = {"estimated_tokens": 185_000, "window_tokens": 180_000, "action": "compact"}
    log.add("context_preflight_blocked", blocked)
    side, summary = _summarize(log)
    done = _compacted(log, side, first, last, "reactive")
    answer(log, "It has one heading.")
    e = log.events
    t = turn_of(CTX, u, e[-1], (event(done),))
    spans = [
        *read_spans(CTX, log, 2),
        t,
        chat_of(CTX, t, side, summary),
        chat_of(CTX, t, e[-3], e[-2]),
    ]
    write(
        root,
        "otel-compaction",
        "A compaction side request is a chat span with threads.model.purpose compaction, and "
        "the compacted event is a turn span event.",
        [log],
        [Sync(BRANCH, log.seq, 0, spans)],
    )


def _clock_skew(root: pathlib.Path) -> None:
    log = Log()
    started(log, [])
    u = user(log, "Hi.")
    r = log.model_request()
    data: Obj = {
        "request_event_id": r["event_id"],
        "content": [{"type": "text", "text": "Hello."}],
        "stop_reason": "end_turn",
        "usage": tokens(10, 2),
        "completeness": "complete",
    }
    # Written by a process whose clock is 5 s behind the one that sent the request.
    resp = log.add("model_response", data, actor="model", time=num(r["time"]) - 5000)
    end = log.add("turn_completed", {"reason": "end_turn"}, time=T0 + 10_000)
    t = turn_of(CTX, u, end)
    write(
        root,
        "otel-clock-skew",
        "A response timed before its request (a clock step between processes): the chat span "
        "is clamped to zero duration and marked threads.clock_skew.",
        [log],
        [Sync(BRANCH, log.seq, 0, [t, chat_of(CTX, t, r, resp)])],
    )


def _long_turn(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE])
    u = user(log, "Read both files.")
    call(log, "read_file", {"path": "a.txt"}, "call_1")
    result(log, "call_1", "a")
    call(log, "read_file", {"path": "b.txt"}, "call_2")
    result(log, "call_2", "b")
    answer(log, "Both read.")
    e = log.events
    t = turn_of(CTX, u, e[-1])
    syncs = [
        Sync(BRANCH, 4, 0, [chat_of(CTX, t, e[2], e[3])]),
        Sync(
            BRANCH,
            9,
            4,
            [tool_of(CTX, t, log, 5, 7, "read_only"), chat_of(CTX, t, e[7], e[8])],
        ),
        Sync(
            BRANCH,
            log.seq,
            9,
            [tool_of(CTX, t, log, 10, 12, "read_only"), chat_of(CTX, t, e[12], e[13]), t],
        ),
    ]
    write(
        root,
        "otel-long-turn-once",
        "One turn with three model calls over three syncs: each span is sent once, by the "
        "first sync after it closes, and the turn span only by the last.",
        [log],
        syncs,
    )

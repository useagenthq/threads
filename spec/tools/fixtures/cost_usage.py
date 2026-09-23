# pyright: strict
"""Usage, cost and cache-break projections at their edges: absent policy, unpriced epochs,
recovered responses, the wire's integer range and exact integer arithmetic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import MAX_SAFE, PARAMS, obj, tokens
from .log import Log
from .pieces import reduce_case, started, user
from .policies import CONTEXT, MODELS, policy
from .projections import cache_breaks, cost

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj


def build(root: pathlib.Path) -> None:
    _unpinned_projections(root)
    _unpriced_epoch(root)
    _recovered_cache_break(root)
    _usage_overflow(root)
    _cost_overflow(root)
    _cache_break_exact(root)
    _max_tokens_not_integer(root)


def _unpinned_projections(root: pathlib.Path) -> None:
    log = Log()
    started(log, [], policy={"models": MODELS})
    for i, cache_read in enumerate((8000, 1000)):
        user(log, f"Question {i + 1}.")
        r = log.model_request()
        usage: Obj = {**tokens(100, 10), "cache_read_tokens": cache_read, "cache_write_tokens": 0}
        log.model_response(r, [{"type": "text", "text": f"Answer {i + 1}."}], "end_turn", usage)
        log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "projections-absent-without-policy",
            "context_compaction",
            "The policy pins priced models but no currency and no context. cost is null: "
            "prices mean nothing without a currency, and a zero would read as free. "
            "cache_breaks is null though reads fell from 8000 to 1000: the projection has no "
            "pinned ttl to judge against.",
        ),
        log,
        {"cost": cost(log), "cache_breaks": cache_breaks(log)},
    )


def _unpriced_epoch(root: pathlib.Path) -> None:
    unpriced = {k: v for k, v in obj(MODELS[0]).items() if k != "price"}
    log = Log()
    started(log, [], policy={"models": [unpriced], "currency": "USD"})
    user(log, "Question.")
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": "Answer."}], "end_turn", tokens(100, 10))
    log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "cost-unpriced-model-incomplete",
            "cancellation_resume",
            "The pinned model declares no price. Its response is known usage but has no known "
            "cost: cost adds nothing for it and is neither complete nor bounded, never an exact "
            "zero.",
        ),
        log,
        {"cost": cost(log)},
    )


def _recovered_cache_break(root: pathlib.Path) -> None:
    log = Log()
    started(log, [], policy=policy(context=CONTEXT))
    for i, cache_read in enumerate((8000, 1000)):
        user(log, f"Question {i + 1}.")
        r = log.model_request()
        usage: Obj = {**tokens(100, 10), "cache_read_tokens": cache_read, "cache_write_tokens": 0}
        reply: list[JsonValue] = [{"type": "text", "text": f"Answer {i + 1}."}]
        if i == 0:
            log.model_response(r, reply, "end_turn", usage)
        else:
            recovered: Obj = {
                "request_event_id": r["event_id"],
                "provider_request_id": "msg_02",
                "content": reply,
                "stop_reason": "end_turn",
                "usage": usage,
                "completeness": "complete",
            }
            log.add("model_response_recovered", recovered, actor="recovery")
        log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "cache-break-recovered-response",
            "context_compaction",
            "The second turn's response was recovered by lookup after a crash. A recovered "
            "response is a turn response like any other, so its cache reads falling from 8000 to "
            "1000 is a break (unknown cause), and its usage counts.",
        ),
        log,
        {"cache_breaks": cache_breaks(log)},
    )


def _usage_overflow(root: pathlib.Path) -> None:
    log = Log()
    started(log, [])
    half = MAX_SAFE // 2 + 1
    for i in range(2):
        user(log, f"Question {i + 1}.")
        r = log.model_request()
        reply: list[JsonValue] = [{"type": "text", "text": f"Answer {i + 1}."}]
        log.model_response(r, reply, "end_turn", tokens(half, 10))
        log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "usage-overflow-counts-unknown",
            "cancellation_resume",
            "Each response reports more than half of 2^53-1 input tokens. The second would take "
            "the input total past the wire's integers, so it adds neither count and is counted "
            "in unknown_responses: a total is never out of range, rounded or saturated.",
        ),
        log,
        {},
    )


def _cost_overflow(root: pathlib.Path) -> None:
    log = Log()
    started(log, [], policy=policy())
    user(log, "Question.")
    r = log.model_request()
    # 4e12 input tokens at 3000 nanos each: 1.2e16 nanos, past 2^53-1.
    reply: list[JsonValue] = [{"type": "text", "text": "Answer."}]
    log.model_response(r, reply, "end_turn", tokens(4_000_000_000_000, 10))
    log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "cost-overflow-projection",
            "cancellation_resume",
            "The known cost passes 2^53-1 nanos. The cost projection is the typed overflow "
            "outcome {error: cost_overflow}, never an out-of-range, rounded or saturated amount.",
        ),
        log,
        {"cost": cost(log)},
    )


def _cache_break_exact(root: pathlib.Path) -> None:
    log = Log()
    started(log, [], policy=policy(context=CONTEXT))
    # 20 x 8556839292003941 < 19 x 9007199254740991 by 9: a drop that doubles can't see.
    for i, cache_read in enumerate((MAX_SAFE, 8_556_839_292_003_941)):
        user(log, f"Question {i + 1}.")
        r = log.model_request()
        usage: Obj = {**tokens(100, 10), "cache_read_tokens": cache_read, "cache_write_tokens": 0}
        reply: list[JsonValue] = [{"type": "text", "text": f"Answer {i + 1}."}]
        log.model_response(r, reply, "end_turn", usage)
        log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "cache-break-exact-integers",
            "context_compaction",
            "Cache reads fall from 2^53-1 to 8556839292003941: 20 x now is 9 less than 19 x "
            "previous, so it is a break. The comparison is exact integer arithmetic; doubles "
            "round both products to the same value and would miss it.",
        ),
        log,
        {"cache_breaks": cache_breaks(log)},
    )


def _max_tokens_not_integer(root: pathlib.Path) -> None:
    log = Log()
    started(log, [], policy=policy(), params={**PARAMS, "max_tokens": True})
    user(log, "Question.")
    log.model_request()
    reduce_case(
        root,
        (
            "cost-max-tokens-not-integer",
            "cancellation_resume",
            "The pinned model_params.max_tokens is true, not an integer, so the attempt has no "
            "output bound. Its request has no response, so cost can't bound it: bounded false. "
            "A boolean is never read as 1.",
        ),
        log,
        {"cost": cost(log)},
    )

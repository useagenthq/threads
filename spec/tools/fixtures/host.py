# pyright: strict
"""Stub-mode and channel-intake cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, aref, sha, tokens
from .jcs import JsonValue, Obj, canonical
from .log import Log, reduce
from .pieces import SEARCH, case, started, user, write_case

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    # stub mode: (tool, args_hash, occurrence) with ordered consumption
    log = Log()
    started(log, [SEARCH])
    user(log, "Search for threads twice, then once more.")
    q: Obj = {"q": "threads"}
    h = sha(canonical(q))
    outs = [b"result A", b"result B"]
    contents: list[JsonValue] = [
        [{"type": "tool_use", "call_id": f"call_{i}", "name": "search_web", "input": q}]
        for i in (1, 2, 3)
    ]
    resp: list[JsonValue] = [
        {"content": c, "stop_reason": "tool_use", "usage": tokens(50, 5)} for c in contents
    ]
    appended: list[JsonValue] = []
    for i in (1, 2, 3):
        cid = f"call_{i}"
        step: list[JsonValue] = [
            {"type": "model_request", "data": {"attempt": 1}},
            {
                "type": "model_response",
                "data": {"content": contents[i - 1], "completeness": "complete"},
            },
            {
                "type": "tool_call",
                "data": {"call_id": cid, "name": "search_web", "input": q},
            },
            {
                "type": "permission_decision",
                "data": {"call_id": cid, "decision": "allow"},
            },
            {"type": "effect_begin", "data": {"call_id": cid, "attempt": 1}},
        ]
        appended += step
        if i < len(outs) + 1:
            done: list[JsonValue] = [
                {
                    "type": "effect_commit",
                    "data": {
                        "call_id": cid,
                        "result_ref": aref(outs[i - 1], "text/plain"),
                    },
                },
                {
                    "type": "tool_result",
                    "data": {
                        "call_id": cid,
                        "is_error": False,
                        "origin": "executed",
                        "preview": outs[i - 1].decode(),
                    },
                },
            ]
            appended += done
    appended.append(
        {
            "type": "effect_resolved",
            "data": {"call_id": "call_3", "outcome": "not_sent", "by": "adapter"},
        }
    )
    write_case(
        root,
        case(
            "stub-occurrence-order",
            "log_fork_test",
            "stub",
            "Stub mode. The same host operation (search_web, same args_hash) runs three times. "
            "Stubs match on (tool, args_hash, occurrence) and are consumed in order, so calls 1 "
            "and 2 get results A and B. Call 3 has no stub: it fails closed with "
            "unmatched_external_op, is settled not_sent, and never goes live.",
            model_script="model.json",
            stub_script="stubs.json",
        ),
        log,
        {
            "outcome": "error",
            "error": {"code": "unmatched_external_op"},
            "state": reduce(log, NOW),
            "appended": appended,
            "stubs": {"consumed": 2, "unmatched": 1},
        },
        extra={
            "model.json": {"responses": resp},
            "stubs.json": {
                "stubs": [
                    {
                        "tool": "search_web",
                        "args_hash": h,
                        "occurrence": i,
                        "output": o.decode(),
                    }
                    for i, o in enumerate(outs)
                ]
            },
        },
    )

    # channel intake: batches keep every item
    write_case(
        root,
        case(
            "channel-batch-items-kept",
            "channels",
            "intake",
            "One webhook carries two items without per-item ids, and is redelivered after a "
            "crash that followed the inbox insert. A second webhook carries two items with "
            "provider item ids. Every item gets its own inbox row before the response (key "
            "<delivery_id>#<index>, or the provider item id); the redelivery is a no-op that is "
            "still answered; no item is lost or merged under a shared key.",
            input={
                "webhooks": [
                    {
                        "channel": "slack",
                        "installation_id": "T024BE7LD",
                        "delivery_id": "Ev01",
                        "items": [
                            {"conversation": "C1", "sender": "U1", "text": "first"},
                            {"conversation": "C1", "sender": "U1", "text": "second"},
                        ],
                    },
                    {
                        "channel": "slack",
                        "installation_id": "T024BE7LD",
                        "delivery_id": "Ev01",
                        "items": [
                            {"conversation": "C1", "sender": "U1", "text": "first"},
                            {"conversation": "C1", "sender": "U1", "text": "second"},
                        ],
                    },
                    {
                        "channel": "whatsapp",
                        "installation_id": "waba_9",
                        "delivery_id": "wh_77",
                        "items": [
                            {
                                "item_id": "wamid.A",
                                "conversation": "15550001",
                                "sender": "15550001",
                                "text": "hi",
                            },
                            {
                                "item_id": "wamid.B",
                                "conversation": "15550001",
                                "sender": "15550001",
                                "text": "there",
                            },
                        ],
                    },
                ]
            },
        ),
        None,
        {
            "outcome": "ok",
            "responses": [200, 200, 200],
            "inbox": [
                {"channel": "slack", "item_key": "Ev01#0"},
                {"channel": "slack", "item_key": "Ev01#1"},
                {"channel": "whatsapp", "item_key": "wamid.A"},
                {"channel": "whatsapp", "item_key": "wamid.B"},
            ],
        },
    )

# pyright: strict
"""Channel intake and host API cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, ALLOW
from .jcs import JsonValue, Obj
from .log import Log
from .pieces import answer, case, render_case, started, user, write_case

if TYPE_CHECKING:
    import pathlib

BOB: Obj = {"issuer": "api", "tenant": "acme", "subject": "bob"}
RUN: Obj = {"agent": "demo", "input": "Deploy it."}


def _msg(conversation: str, sender: str, message: str, item_id: str | None = None) -> Obj:
    item: Obj = {"conversation": conversation, "sender": sender, "text": message}
    return item if item_id is None else {"item_id": item_id, **item}


def _hook(installation: str, delivery: str, items: list[JsonValue], **more: JsonValue) -> Obj:
    return {
        "channel": "slack",
        "installation_id": installation,
        "delivery_id": delivery,
        "items": items,
        **more,
    }


def _request(principal: Obj, key: str, body: Obj) -> Obj:
    return {"principal": principal, "idempotency_key": key, "body": body}


def _host_reply(root: pathlib.Path) -> None:
    """A host-issued channel_send settles after the turn; the next request
    renders the model's own reply once and nothing of the send."""
    log = Log()
    started(log, [])
    user(log, "Hello")
    answer(log, "Hello back.")
    request = next(e for e in reversed(log.events) if e["type"] == "model_request")
    call_id = f"send_{log.seq - 1}_0"
    op: Obj = {"kind": "text", "text": "Hello back.", "address": "C1", "installation_id": "T1"}
    log.tool_call(request, call_id, "channel_send", op)
    log.add("permission_decision", {"call_id": call_id, **ALLOW})
    log.add("effect_begin", {"call_id": call_id, "attempt": 1})
    ref = log.art(b"C1:1712.000100", "text/plain")
    log.add(
        "effect_commit",
        {"call_id": call_id, "result_ref": ref, "provider_receipt": "C1:1712.000100"},
    )
    log.add(
        "tool_result",
        {
            "call_id": call_id,
            "is_error": False,
            "completeness": "complete",
            "preview": "sent C1:1712.000100",
            "origin": "executed",
        },
    )
    user(log, "Thanks")
    render_case(
        root,
        (
            "render-host-channel-send-hidden",
            "channels",
            "A host-issued channel_send reply (a tool_call no model response proposed) is "
            "recorded as an effect but never rendered: the model's own final text is already in "
            "history, and a host reply is not a model tool call, so no orphan tool result.",
        ),
        log,
    )


def build(root: pathlib.Path) -> None:
    _host_reply(root)
    hello = [_msg("C1", "U1", "hello")]
    write_case(
        root,
        case(
            "channel-forged-rejected",
            "channels",
            "intake",
            "A webhook that fails the adapter's verification is answered 401 and nothing is "
            "stored, before any append; the authentic delivery after it is stored once.",
            input={
                "webhooks": [
                    _hook("T024BE7LD", "Ev07", hello, forged=True),
                    _hook("T024BE7LD", "Ev07", hello),
                ]
            },
        ),
        None,
        {
            "outcome": "ok",
            "responses": [401, 200],
            "inbox": [{"channel": "slack", "item_key": "Ev07#0"}],
            "threads": 1,
        },
    )
    write_case(
        root,
        case(
            "channel-redelivery-no-dup",
            "channels",
            "intake",
            "The provider redelivers the same message three times, including after a crash "
            "before and after our response. Each redelivery is answered and inserts nothing: "
            "one inbox row, so one run and one reply.",
            input={"webhooks": [_hook("T024BE7LD", "Ev09", hello)] * 3},
        ),
        None,
        {
            "outcome": "ok",
            "responses": [200, 200, 200],
            "inbox": [{"channel": "slack", "item_key": "Ev09#0"}],
            "threads": 1,
        },
    )
    write_case(
        root,
        case(
            "channel-identity-cross-workspace",
            "channels",
            "intake",
            "The same channel, user id and delivery id arrive from two workspaces. They are "
            "different installations, so different inbox rows and different threads: message "
            "content or ids never select another workspace's thread.",
            input={
                "webhooks": [
                    _hook("T024BE7LD", "Ev11", hello),
                    _hook("T999OTHER", "Ev11", hello),
                ]
            },
        ),
        None,
        {
            "outcome": "ok",
            "responses": [200, 200],
            "inbox": [
                {"channel": "slack", "item_key": "Ev11#0"},
                {"channel": "slack", "item_key": "Ev11#0"},
            ],
            "threads": 2,
        },
    )
    write_case(
        root,
        case(
            "host-run-lost-response-replay",
            "channels",
            "host",
            "A client POSTs /v1/runs, the user_input becomes durable and the response is lost. "
            "The retry with the same Idempotency-Key and body replays the same receipt (the same "
            "run_id, the user_input's event_id) and starts nothing; the same key with another "
            "body is idempotency_key_reused.",
            input={
                "requests": [
                    _request(ALICE, "k-1", RUN),
                    _request(ALICE, "k-1", RUN),
                    _request(ALICE, "k-1", {**RUN, "input": "Deploy staging."}),
                ]
            },
        ),
        None,
        {
            "outcome": "ok",
            "api": [
                {"status": 202, "receipt": 0},
                {"status": 202, "receipt": 0},
                {"status": 409, "code": "idempotency_key_reused"},
            ],
            "user_inputs": 1,
        },
    )
    write_case(
        root,
        case(
            "host-run-idempotency-other-principal",
            "channels",
            "host",
            "Principal B of the same tenant reuses principal A's Idempotency-Key with the same "
            "body. B gets idempotency_key_principal_mismatch, never A's receipt, and nothing "
            "starts; B's own key is its own run.",
            input={
                "requests": [
                    _request(ALICE, "k-1", RUN),
                    _request(BOB, "k-1", RUN),
                    _request(BOB, "k-2", RUN),
                ]
            },
        ),
        None,
        {
            "outcome": "ok",
            "api": [
                {"status": 202, "receipt": 0},
                {"status": 409, "code": "idempotency_key_principal_mismatch"},
                {"status": 202, "receipt": 2},
            ],
            "user_inputs": 2,
        },
    )

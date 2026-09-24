# pyright: strict
"""Team op vectors for the operator's own rules (design §4.4; spec/schema/README.md, "Teams",
"Operator requests"): an idempotency key replays its first request's outcome and appends
nothing; another principal or body under that key is refused, recorded without the key; and an
operator start's chosen fields, whose refusal detail the team log keeps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, text
from .dynamic import SPECIALIST
from .ops_member import start
from .ops_send import send
from .team_ops_dynamic import DYNAMIC_HASH, NEW_THREAD
from .team_ops_worlds import BOB, REQUESTS, Vec, ended, operator, refused, running
from .team_pieces import WRITER, ref

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World

P = "message_policy_decided"
REFUSED = ["operator_request", "operator_refused"]
BODY: Obj = {"agent": "writer", "task": "Draft the summary."}
KEY = "start-1"


def _started() -> World:
    """The operator started writer-1 under the key start-1."""
    w = running()
    start(w, "team", {**operator(REQUESTS[0], BODY, KEY), "thread_id": NEW_THREAD})
    return w


def _retry(body: Obj = BODY, who: Obj | None = None) -> Obj:
    """The same key again, under a new request id: the caller never saw the first outcome."""
    inp = operator(REQUESTS[1], body, KEY) if who is None else operator(REQUESTS[1], body, KEY, who)
    return {**inp, "thread_id": NEW_THREAD}


def _keyed() -> list[Vec]:
    return [
        Vec(
            "start-operator-key-replayed",
            "4.4",
            "A retry of team.start with the key, principal and body of a recorded request: the "
            "first request's outcome, read from the team log. Nothing is appended.",
            _started(),
            "start",
            "team",
            _retry(),
            {"member": WRITER, "status": "started"},
            {},
        ),
        Vec(
            "start-operator-key-reused",
            "4.4",
            "The key again with another body: refused idempotency_key_reused. The request is "
            "recorded without its key, which stays bound to the first request (no receipt row).",
            _started(),
            "start",
            "team",
            _retry({**BODY, "task": "Draft it again."}),
            refused("idempotency_key_reused"),
            {"team": REFUSED},
        ),
        Vec(
            "start-operator-key-principal-mismatch",
            "4.4",
            "The key again from another principal of the tenant: refused "
            "idempotency_key_principal_mismatch, checked before the body.",
            _started(),
            "start",
            "team",
            _retry({**BODY, "task": "Draft it again."}, BOB),
            refused("idempotency_key_principal_mismatch"),
            {"team": REFUSED},
        ),
        _refusal_replayed(),
    ]


def _refusal_replayed() -> Vec:
    body: Obj = {"to": ref("researcher-1"), "text": "Status?"}
    w = ended()
    send(w, "team", operator(REQUESTS[0], body, "send-1"))
    return Vec(
        "send-operator-key-replayed-refusal",
        "4.4",
        "A refused request is an outcome too: the retry under its key returns the recorded "
        "refusal (member_ended) and appends nothing.",
        w,
        "send",
        "team",
        operator(REQUESTS[1], body, "send-1"),
        refused("member_ended"),
        {},
    )


def _dynamic() -> list[Vec]:
    tools = [text(t) for t in arr(SPECIALIST["tools"])]
    given: Obj = {"templates": {"specialist": SPECIALIST}}
    body: Obj = {"agent": "specialist", "task": "Is INV-1002 paid?"}
    bad: Obj = {**body, "tools": ["git_push"]}
    detail: Obj = {"field": "tools", "reason": "not_allowed", "allowed": list(tools)}
    return [
        Vec(
            "start-operator-dynamic",
            "4.4, 4.10",
            "team.start with a dynamic agent's fields: member_started in the team log records "
            "the define and the label, as a lead's start does.",
            running(),
            "start",
            "team",
            {
                **operator(REQUESTS[0], {**body, "label": "invoice checker", "model": "strong"}),
                "thread_id": NEW_THREAD,
                "config_hash": DYNAMIC_HASH,
            },
            {"member": ref("specialist-1"), "status": "started"},
            {"team": ["operator_request", P, "member_started", "message_sent"]},
            given=given,
        ),
        Vec(
            "start-operator-dynamic-tool-not-allowed",
            "4.4, 4.10",
            "An operator start's tool outside the template: operator_refused carries the "
            "invalid_definition detail, so a replay under a key returns it whole.",
            running(),
            "start",
            "team",
            {**operator(REQUESTS[0], bad), "thread_id": NEW_THREAD, "config_hash": DYNAMIC_HASH},
            {**refused("invalid_definition"), "detail": detail},
            {"team": ["operator_request", P, "operator_refused"]},
            given=given,
        ),
    ]


def operator_vectors() -> list[Vec]:
    return [*_keyed(), *_dynamic()]

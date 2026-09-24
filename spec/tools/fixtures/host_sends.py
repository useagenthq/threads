# pyright: strict
"""The host's own channel sends (spec/schema/README.md, "Channel replies", "The host's calls are
the host's"): a channel_send tool_call no response asked for is the host's, and a park on its
effect is never the agent's run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, BRANCH, DAY, T0, obj
from .log import Log
from .memory import catalog_spec
from .pieces import answer, call, reduce_case, started, user
from .run_end import run_projection

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

FAM = "permissions_approvals"
ASK = catalog_spec("ask_user", "read_only")
SEND = "question_call_1_0"


def build(root: pathlib.Path) -> None:
    log = Log()
    started(log, [ASK])
    user(log, "Paint it.")
    asked = call(log, "ask_user", {"question": "Which color?", "options": ["red", "blue"]})
    question: Obj = {"kind": "input", "id": "call_1"}
    log.add("parked", {"address": question, "reason": "awaiting_input", "expires_at": T0 + DAY})
    # The host's question send: a channel_send no tool_use asked for, left in doubt.
    log.add(
        "tool_call",
        {
            "call_id": SEND,
            "name": "channel_send",
            "input": {"kind": "text", "text": "Which color?", "address": "C1"},
            "request_event_id": obj(asked["data"])["request_event_id"],
        },
    )
    log.add(
        "permission_decision",
        {"call_id": SEND, "decision": "allow", "source": "policy", "rule_id": "channel_delivery"},
    )
    log.add("effect_begin", {"call_id": SEND, "attempt": 1})
    log.add("effect_unknown", {"call_id": SEND, "reason": "transport_error"})
    address: Obj = {"kind": "effect", "id": f"{BRANCH}:{SEND}"}
    log.add("parked", {"address": address, "reason": "effect_unknown"})
    ans = log.add(
        "tool_result",
        {
            "call_id": "call_1",
            "is_error": False,
            "completeness": "complete",
            "preview": "blue",
            "origin": "answered",
        },
        actor="user",
        principal=ALICE,
    )
    log.add("resumed", {"address": question, "cause_event_id": ans["event_id"]})
    answer(log, "Blue it is.")
    reduce_case(
        root,
        (
            "host-send-park-is-not-the-run",
            FAM,
            "The host's question send (a channel_send no tool_use asked for) is left in doubt and "
            "parked for a human; the asker answers and the turn ends end_turn. The run is "
            "completed: a park on the host's own send is never the agent's. The branch still "
            "shows the park.",
        ),
        log,
        {"run": run_projection(log.events)},
    )

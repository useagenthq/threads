"""One team request under its writer (spec/schema/README.md, "Teams"): a model call (its pending
tool_call, call.py) or an operator request (its operator_request in the team log, operator.py).
The ops decide the same way for both; what differs (who sends, the grant, how a member is named,
how the outcome is recorded) is here. Reference: spec/tools/fixtures/ops_request.py."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue

from threads.store.conn import Conn

if TYPE_CHECKING:
    from threads.team.batch import Batch
    from threads.team.call import Refusal
    from threads.team.mail import PutText
    from threads.team.rows import MemberRow, TeamRow

type PolicyOp = Literal["start", "send", "ask", "monitor", "cancel"]
"""The ops the Phase 1 policy decides."""


@dataclass(frozen=True, slots=True)
class Target:
    """The member an op addresses: its name (the decision's target) and its row, read when due."""

    name: str
    row: "Callable[[], MemberRow | Refusal]"


@dataclass(frozen=True, slots=True)
class Request:
    conn: Conn
    batch: "Batch"
    put: "PutText"
    team: "TeamRow"
    sender: JsonValue
    """The sender: the caller's MemberRef, or `{operator: request_id}`."""
    provenance: JsonValue
    causal: JsonValue
    """The request that causes its mail: the tool_call, or the operator_request."""
    mail_id: str
    """`<sender branch_id>:<call_id or request_id>`."""
    own: "MemberRow | None"
    """The sender's own row; None for the operator."""
    decide: "Callable[[PolicyOp, str], Refusal | None]"
    """Records the Phase 1 decision on a target: the team's grant, else default deny."""
    parent: Callable[[str], JsonValue]
    """member_started.parent, given member_started's own event id."""
    refuse: "Callable[[Refusal], dict[str, JsonValue]]"
    """Records a refusal (the call's tool_result, or operator_refused) and returns it."""
    done: Callable[[dict[str, JsonValue]], dict[str, JsonValue]]
    """Records a success (the call's tool_result; an operator's needs nothing) and returns it."""

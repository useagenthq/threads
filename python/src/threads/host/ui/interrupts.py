"""A parked run's open items as AG-UI interrupts (spec/schema/ui/README.md, "Opening and
closing"). Each answer's schema is host-api.v1.schema.json's own (a test keeps them equal)."""

from typing import Final

from pydantic import JsonValue

from threads.host.ui.facts import RunFacts
from threads.log import ParkAddress
from threads.log.instant import iso

APPROVAL_DECISION_SCHEMA: Final[JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision"],
    "properties": {
        "decision": {"enum": ["grant", "deny"]},
        "remember_rule": {
            "description": (
                "grant only: appended as permission_rule_added. Must be one of the challenge's"
                " suggested_rules."
            ),
            "$ref": "urn:threads:schema:events:v1#/$defs/PermissionRule",
        },
        "reason": {"type": "string"},
    },
}

ANSWER_SCHEMA: Final[JsonValue] = {
    "description": "The answer to an ask_user question: text, or the chosen options.",
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {
        "answer": {
            "oneOf": [
                {"type": "string", "minLength": 1},
                {"type": "array", "minItems": 1, "items": {"type": "string"}},
            ]
        }
    },
}


def answerable(address: ParkAddress) -> bool:
    """Whether a park waits on a human's answer the UI can give (an approval or a question)."""
    return address.kind in ("approval", "input")


def interrupts(pending: tuple[ParkAddress, ...], facts: RunFacts) -> list[JsonValue]:
    return [_interrupt(a, facts) for a in pending]


def _interrupt(address: ParkAddress, facts: RunFacts) -> JsonValue:
    if address.kind == "approval":
        challenge = facts.challenge(address.id)
        if challenge is not None:
            call = facts.call(challenge.call_id)
            return {
                "id": address.id,
                "reason": "tool_approval",
                "toolCallId": challenge.call_id,
                "message": f"approve {'the call' if call is None else call.name}",
                "responseSchema": APPROVAL_DECISION_SCHEMA,
                "expiresAt": iso(challenge.expires_at),
            }
    if address.kind == "input":
        call = facts.call(address.id)
        question = None if call is None else call.input.get("question")
        return {
            "id": address.id,
            "reason": "user_input",
            "toolCallId": address.id,
            "message": question if isinstance(question, str) else "",
            "responseSchema": ANSWER_SCHEMA,
        }
    return {"id": address.id, "reason": facts.park_reason(address) or address.kind}

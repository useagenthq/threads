"""One budget per simulated conversation (spec lane 32, B.5): `live.budget` covers every agent run
and every simulated-user run of the case, the prefix re-drive included. Before each run the runner
passes what is left, field by field. Only fields whose remainder is >= 1 are passed (Budget fields
are PosInt); a field the author set whose remainder is <= 0 ends the case."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from threads.log import Budget

if TYPE_CHECKING:
    from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class Spent:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    """turn_completed events appended during this conversation, on both threads."""
    cost_nanos: int = 0
    """The conservative upper bound: unknown usage counts against the budget."""
    wall_ms: int = 0
    """One monotonic clock, started before the conversation's first run."""


def _left(limit: int | None, used: int) -> int | None:
    return None if limit is None else limit - used


def _set(value: object) -> int | None:
    """A Budget field's value, or None when the author left it out (the MISSING sentinel)."""
    return value if isinstance(value, int) else None


def remaining_budget(budget: Budget, spent: Spent) -> Budget | None:
    """What the conversation may still spend, or None when a field the author set is used up."""
    rest: dict[str, JsonValue] = {}
    pairs = (
        ("max_cost_nanos", _left(_set(budget.max_cost_nanos), spent.cost_nanos)),
        ("max_input_tokens", _left(_set(budget.max_input_tokens), spent.input_tokens)),
        ("max_output_tokens", _left(_set(budget.max_output_tokens), spent.output_tokens)),
        ("max_model_requests", _left(_set(budget.max_model_requests), spent.requests)),
        ("max_turns", _left(_set(budget.max_turns), spent.turns)),
        ("max_wall_ms", _left(_set(budget.max_wall_ms), spent.wall_ms)),
    )
    for field, left in pairs:
        if left is None:
            continue
        if left < 1:
            return None
        rest[field] = left
    return Budget.model_validate(rest)

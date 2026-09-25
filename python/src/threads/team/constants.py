"""The Teams constants (spec/schema/README.md, "Teams", Constants), the same in both runtimes.
Each operation that uses one takes it as a parameter defaulting to this value, so tests inject
others."""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class TeamConstants:
    inline_cap_bytes: int = 16_384
    """A text value larger than this many UTF-8 bytes becomes an artifact ref."""
    busy_bound_ms: int = 5_000
    """How long an operator request retries a held team-log lease before `refused{busy}`."""
    claim_ttl_ms: int = 30_000
    """How long a `mail.claim` holds a pending row."""
    ask_wait_default_ms: int = 120_000
    """The default deadline of `ask` and `wait`, and their cap."""
    wake_poll_in_process_ms: int = 250
    wake_poll_cross_process_ms: int = 1_000
    setup_attempts: int = 5
    """How many times in a row a member's setup may fail for now before it ends `setup_failed`."""


TEAM_CONSTANTS: Final = TeamConstants()

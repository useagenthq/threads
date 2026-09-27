"""A branch that doesn't read is not read again until its back-off passes: 1 s, doubling to
1 h, and reset when its head moves (something appended, which may have repaired it)."""

from dataclasses import dataclass
from typing import Final

_FIRST_MS: Final = 1_000
_MAX_MS: Final = 3_600_000


@dataclass(frozen=True, slots=True)
class _Streak:
    head: int
    wait_ms: int
    until_ms: int


class Backoff:
    def __init__(self) -> None:
        self._streaks: dict[str, _Streak] = {}

    def waiting(self, branch_id: str, head: int, now: int) -> bool:
        """True while the branch is backed off at the same head."""
        streak = self._streaks.get(branch_id)
        if streak is None:
            return False
        if streak.head != head:
            del self._streaks[branch_id]
            return False
        return now < streak.until_ms

    def failed(self, branch_id: str, head: int, now: int) -> None:
        before = self._streaks.get(branch_id)
        same = before is not None and before.head == head
        wait = min(before.wait_ms * 2, _MAX_MS) if before is not None and same else _FIRST_MS
        self._streaks[branch_id] = _Streak(head, wait, now + wait)

    def cleared(self, branch_id: str) -> None:
        self._streaks.pop(branch_id, None)

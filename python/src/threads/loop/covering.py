"""A budget covering a thread: its own, or one inherited from an ancestor."""

from dataclasses import dataclass
from typing import Literal

from threads.log import Budget, ThreadId


@dataclass(frozen=True, slots=True)
class Covering:
    """A budget over this thread: its own (`thread`, `run`) or an ancestor's."""

    budget_id: str
    budget: Budget
    scope: Literal["thread", "run", "ancestor"]
    owner: ThreadId | None = None
    """The ancestor whose budget this is; None for this thread's own."""

# pyright: strict
"""The staged Phase 0 replay case (spec/schema/README.md, "Teams", the replay rule): the
pending_wakes row a legacy wake leaves. Phase 0 (the legacy wake) moves it into the corpus; the
team half is team_replay."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .pieces import reduce_case
from .team_index import pending_wakes
from .team_steps import FAM
from .wakes import one_append_log

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    log = one_append_log()
    reduce_case(
        root,
        (
            "legacy-wake-pending-row",
            FAM,
            "legacy-wake-in-the-late-result-append's log: the child that finished leaves no "
            "pending_wakes row, and the one still running keeps its row.",
        ),
        log,
        {"pending_wakes": pending_wakes([log])},
    )

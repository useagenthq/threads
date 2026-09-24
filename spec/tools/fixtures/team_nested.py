# pyright: strict
"""The staged nested-lead case (spec/schema/README.md, "Teams", "Feeds"): a member whose own
definition has a team is a lead too. Its events are in both teams' feeds; its team's log is only
in its own team's."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import text
from .log import Log
from .pieces import user
from .team_pieces import (
    MEMBER_THREAD,
    RESEARCHER,
    TENANT,
    lead_log,
    team_log,
)
from .team_steps import materialize, start, write_team

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

INNER = "0192c000-0000-7000-8000-000000000002"
INNER_LOG = ("0192b000-0000-7000-8000-0000000000b6", "0192a000-0000-7000-8000-0000000000b6")


def build(root: pathlib.Path) -> None:
    lead = lead_log()
    root_event = text(user(lead, "Research batteries.")["event_id"])
    started_id, task = start(lead, root_event, RESEARCHER, "c1")
    inner: Obj = {"id": INNER, "log_branch_id": INNER_LOG[0]}
    researcher = materialize(started_id, task, team=inner)
    opened = Log(INNER_LOG[0], thread=INNER_LOG[1])
    nested_lead: Obj = {"tenant": TENANT, "team": INNER, "name": "researcher", "generation": 1}
    opened.add("team_opened", {"team": INNER, "lead": nested_lead, "lead_thread_id": MEMBER_THREAD})
    write_team(
        root,
        "team-nested-lead-feeds",
        "researcher-1 is a member of the lead's team and, because its definition has a team, "
        "the lead of its own: its thread_started carries both the team_member parent and the "
        "team, and its first append opens the inner team log. The index holds both of its rows "
        "(member researcher-1 in the outer team, lead researcher in the inner), both running on "
        "its branch. Every event of its log has a team_feed row under both team_ids; the inner "
        "team log's events are only in the inner feed, and the outer team log's only in the "
        "outer. The tree counts its thread once.",
        {"inner": opened, "lead": lead, "researcher": researcher, "team": team_log()},
    )

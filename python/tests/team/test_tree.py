"""Tree cost over a team (spec/schema/README.md, "Tree walks with teams"): a lead's members count
from their own logs, one in the starting window counts zero, and a missing or forged member log
makes the tree log_corrupt."""

import asyncio

import pytest
from pydantic import JsonValue
from team.team_kit import (
    LEAD,
    MEMBER,
    TEAM,
    Line,
    branch_of,
    holding,
    lift_refusal,
    rechain,
    staged,
)
from thread.rewrite_log import rewrite_log

from threads.agents.store import Store, open_store
from threads.log import BranchId, Cost, ParseError
from threads.result import Err, Ok
from threads.store import ForkRequest
from threads.team.members import PENDING, open_member, team_members
from threads.team.rebuild import rebuild_team_index
from threads.thread.read import read_log
from threads.thread.usage import tree_cost

POLICY: JsonValue = {
    "currency": "USD",
    "models": [
        {
            "provider": "scripted",
            "name": "scripted-1",
            "context_window": 200000,
            "max_output_tokens": 1024,
            "input_billing_bound": "context_window",
            "price": {"input": 3000, "output": 15000},
        }
    ],
}
"""The price both languages pin on the staged logs for the tree totals."""
SETTLE_TREE: JsonValue = {
    "currency": "USD",
    "known_nanos": 1800000,
    "upper_bound_nanos": 1800000,
    "complete": True,
    "bounded": True,
}
"""team-settle-wakes-lead, every log priced: the lead's 260 in and 40 out, the researcher's 90
in and 10 out, at 3000 and 15000 nanos per token. The TypeScript suite pins the same bytes."""


def _priced(events: list[Line]) -> list[Line]:
    started = events[0]
    data = started["data"]
    assert isinstance(data, dict)
    started["data"] = {**data, "policy": POLICY}
    return events


async def _tree(store: Store) -> Ok[Cost | None] | Err[ParseError]:
    sq = await open_store(store)
    assert await rebuild_team_index(sq, TEAM) == Ok(None)
    lead = await read_log(store, branch_of(LEAD))
    assert isinstance(lead, Ok)
    return await tree_cost(store, LEAD, lead.value)


def _cost(found: Ok[Cost | None] | Err[ParseError]) -> JsonValue:
    assert isinstance(found, Ok), found
    assert found.value is not None
    return found.value.model_dump(mode="json")


def test_the_tree_adds_each_member_from_its_own_log(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)
    logs = staged("team-settle-wakes-lead")
    logs = {k: v if k == "team" else rechain(v, _priced) for k, v in logs.items()}
    assert _cost(asyncio.run(_run(logs))) == SETTLE_TREE


def test_only_the_lead_priced_leaves_the_member_unpriced(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)
    logs = staged("team-settle-wakes-lead")
    logs["lead"] = rechain(logs["lead"], _priced)
    cost = _cost(asyncio.run(_run(logs)))
    lead_only = {"known_nanos": 1380000, "upper_bound_nanos": 1380000}
    assert cost == {**_as_dict(SETTLE_TREE), **lead_only, "complete": False, "bounded": False}


def test_a_member_in_the_starting_window_counts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)
    logs = staged("team-tree-starting-member-pending")
    logs["lead"] = rechain(logs["lead"], _priced)

    async def main() -> tuple[JsonValue, object]:
        store = await holding(logs)
        tree = _cost(await _tree(store))
        lead = await read_log(store, branch_of(LEAD))
        assert isinstance(lead, Ok)
        (member,) = await team_members(store, lead.value)
        return tree, await open_member(store, lead.value, member)

    tree, opened = asyncio.run(main())
    assert tree == {**_as_dict(SETTLE_TREE), "known_nanos": 960000, "upper_bound_nanos": 960000}
    assert opened == Ok(PENDING)


def test_a_missing_member_log_after_its_notice_is_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)
    found = asyncio.run(_run(staged("team-tree-missing-branch-after-notice-rejected")))
    assert isinstance(found, Err)
    assert found.error.code == "log_corrupt"


def test_an_operator_started_member_names_the_leads_thread_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lift_refusal(monkeypatch)
    logs = {
        k: v if k == "team" else rechain(v, _priced)
        for k, v in staged("team-operator-start-and-wait").items()
    }
    cost = _cost(asyncio.run(_run(logs)))
    assert _as_dict(cost)["complete"] is True


def test_a_forged_backlink_is_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The member's thread_started names another event of the lead than its member_started."""
    lift_refusal(monkeypatch)

    async def main() -> Ok[Cost | None] | Err[ParseError]:
        store = await holding(staged("team-settle-wakes-lead"))
        assert await rebuild_team_index(await open_store(store), TEAM) == Ok(None)

        def forge(lines: list[Line]) -> list[Line]:
            data = lines[0]["data"]
            assert isinstance(data, dict)
            parent = data["parent"]
            assert isinstance(parent, dict)
            other = "0192e001-0000-7000-8000-000000000002"
            lines[0]["data"] = {**data, "parent": {**parent, "event_id": other}}
            return lines

        await rewrite_log(store, MEMBER, forge)
        lead = await read_log(store, branch_of(LEAD))
        assert isinstance(lead, Ok)
        return await tree_cost(store, LEAD, lead.value)

    found = asyncio.run(main())
    assert isinstance(found, Err)
    assert found.error.code == "log_corrupt"
    assert "member researcher-1" in found.error.message


def test_a_fork_of_the_lead_still_counts_its_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """The member's backlink names the branch its member_started is on (main), not the fork the
    walk starts from. A repair fork at seq 10 (the start call's result): the fork's own 540,000
    plus the researcher's 420,000, as TypeScript pins."""
    lift_refusal(monkeypatch)
    fork = BranchId("0192b000-0000-7000-8000-0000000000f1")

    async def main() -> Ok[Cost | None] | Err[ParseError]:
        logs = {
            k: v if k == "team" else rechain(v, _priced)
            for k, v in staged("team-settle-wakes-lead").items()
        }
        store = await holding(logs)
        sq = await open_store(store)
        assert await rebuild_team_index(sq, TEAM) == Ok(None)
        request = ForkRequest(branch_of(LEAD), 10, fork, {"reason": "repair"})
        assert isinstance(await sq.fork(request, "test", lambda: 0), Ok)
        forked = await read_log(store, fork)
        assert isinstance(forked, Ok)
        return await tree_cost(store, LEAD, forked.value)

    known = {"known_nanos": 960000, "upper_bound_nanos": 960000}
    assert _cost(asyncio.run(main())) == {**_as_dict(SETTLE_TREE), **known}


async def _run(logs: dict[str, bytes]) -> Ok[Cost | None] | Err[ParseError]:
    return await _tree(await holding(logs))


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value

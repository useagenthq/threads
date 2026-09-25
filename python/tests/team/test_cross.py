"""Semantic rule 43 across a team's logs: every accepted team case passes, and each rejected
team case breaks exactly at its pinned log and seq, whether one log's rules or rule 43 catch it."""

import json
from collections.abc import Callable

import pytest
from pydantic import JsonValue
from team.team_kit import CASES, case_logs, rechain, team_cases, verified

from threads.result import Ok
from threads.team.cross import TeamLogEvents, check_team_logs


def _check(logs: dict[str, bytes]) -> tuple[str, int] | None:
    """The first failure as (label, seq), logs in label order."""
    by_branch: dict[str, str] = {}
    team: list[TeamLogEvents] = []
    for label in sorted(logs):
        log = verified(logs[label])
        if not isinstance(log, Ok):
            # A one-log rule broke first (rule 37's member half is a team case).
            assert log.error.seq is not None, label
            return (label, log.error.seq)
        branch = log.value.segments[-1].header.branch_id
        assert log.value.fold.thread_id is not None
        by_branch[branch] = label
        team.append(TeamLogEvents(log.value.fold.thread_id, branch, log.value.fold.events))
    found = check_team_logs(team)
    return None if found is None else (by_branch[found.branch_id], found.seq)


def _expected(case: str) -> tuple[str, int] | None:
    expected = json.loads((CASES / case / "expected.json").read_text())
    error = expected.get("error")
    if expected["outcome"] == "ok" or error is None or error["code"] != "invalid_transition":
        return None
    return (error["log"], error["seq"])


@pytest.mark.parametrize("case", team_cases())
def test_team_cases(case: str) -> None:
    assert _check(case_logs(case)) == _expected(case)


def test_the_rule_43_cases_are_among_them() -> None:
    pinned = {c: _expected(c) for c in team_cases()}
    assert pinned["team-receipt-envelope-mismatch-rejected"] == ("researcher", 3)
    assert pinned["team-ask-bounce-without-ask-rejected"] == ("researcher", 9)


type Line = dict[str, JsonValue]


def _edit_first(kind: str, edit: Callable[[Line], None]) -> Callable[[list[Line]], list[Line]]:
    def apply(events: list[Line]) -> list[Line]:
        e = next(e for e in events if e["type"] == kind)
        edit(e)
        return events

    return apply


def _data(line: Line) -> Line:
    data = line["data"]
    assert isinstance(data, dict)
    return data


def test_a_mail_sent_from_a_log_its_from_does_not_name() -> None:
    """The lead's task names researcher-1 as its sender."""
    logs = case_logs("team-settle-wakes-lead")

    def forge(e: Line) -> None:
        env = _obj(_data(e)["envelope"])
        env["from"] = {**_obj(env["from"]), "name": "researcher-1"}

    logs["lead"] = rechain(logs["lead"], _edit_first("message_sent", forge))
    assert _check(logs) == ("lead", 9)


def _obj(value: JsonValue) -> Line:
    assert isinstance(value, dict)
    return value


def test_a_member_whose_parent_is_not_its_member_started() -> None:
    logs = case_logs("team-settle-wakes-lead")

    def forge(e: Line) -> None:
        parent = _obj(_data(e)["parent"])
        _data(e)["parent"] = {**parent, "event_id": "0192e001-0000-7000-8000-0000000000ee"}

    logs["researcher"] = rechain(logs["researcher"], _edit_first("thread_started", forge))
    assert _check(logs) == ("researcher", 1)


def test_a_task_taken_by_another_principal() -> None:
    logs = case_logs("team-settle-wakes-lead")

    def forge(e: Line) -> None:
        actor = _obj(e["actor"])
        e["actor"] = {**actor, "principal": {"issuer": "api", "tenant": "acme", "subject": "bob"}}

    logs["researcher"] = rechain(logs["researcher"], _edit_first("user_input", forge))
    assert _check(logs) == ("researcher", 2)

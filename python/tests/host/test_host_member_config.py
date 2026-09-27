"""host(members=...) setup refusals (spec/api.json `host.members`; lane 29D): what a host member
may not be, and what a message_policy rule may not allow to or between host members. Mirrors
TypeScript's host/test/host-member-config.test.ts."""

from typing import TYPE_CHECKING

import pytest
from host.host_members_kit import billing, rule, serving, support
from pydantic import BaseModel

from threadsai import RunContext, agent, scripted_model, tool
from threadsai.agents.config import ConfigError
from threadsai.host.members import HostMemberOptions

if TYPE_CHECKING:
    from threadsai.agents.factory import Agent


def test_members_naming_an_agent_the_host_does_not_define_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        serving({"billing": billing()}, {"payroll": HostMemberOptions()})
    assert caught.value.code == "invalid_config"
    assert "members.payroll: this host defines no agent payroll" in str(caught.value)


def test_a_host_member_with_a_team_is_refused() -> None:
    helper = agent(name="helper", model=scripted_model({"responses": []}))
    lead = agent(name="billing", model=scripted_model({"responses": []}), team=[helper])
    with pytest.raises(ConfigError) as caught:
        serving({"billing": lead}, {"billing": HostMemberOptions()})
    assert caught.value.code == "invalid_config"
    assert "members.billing" in str(caught.value)
    assert "has a team of its own" in str(caught.value)


def test_a_host_member_with_handoffs_is_refused_handoff_in_team() -> None:
    other = agent(name="other", model=scripted_model({"responses": []}))
    hands = agent(name="billing", model=scripted_model({"responses": []}), handoffs=[other])
    with pytest.raises(ConfigError) as caught:
        serving({"billing": hands, "other": other}, {"billing": HostMemberOptions()})
    assert caught.value.code == "handoff_in_team"
    assert "members.billing" in str(caught.value)


def test_a_host_member_that_turns_compaction_off_is_refused() -> None:
    never = agent(
        name="billing",
        model=scripted_model({"responses": []}),
        context={
            "compact": {
                "trigger": {"permille": 1000},
                "keep_tail": {"tokens": 20_000},
                "max_failures": 3,
            }
        },
    )
    with pytest.raises(ConfigError) as caught:
        serving({"billing": never}, {"billing": HostMemberOptions()})
    assert caught.value.code == "invalid_config"
    assert "turns compaction off" in str(caught.value)


def test_a_host_member_whose_token_trigger_is_past_its_window_is_refused() -> None:
    """The other spelling of the same thing. Only the permille form was covered, in either
    language, which is how TypeScript came to accept an agent this side refused."""
    never = agent(
        name="billing",
        model=scripted_model({"responses": []}),
        context={
            "compact": {
                "trigger": {"tokens": 10_000_000},
                "keep_tail": {"tokens": 20_000},
                "max_failures": 3,
            }
        },
    )
    with pytest.raises(ConfigError) as caught:
        serving({"billing": never}, {"billing": HostMemberOptions()})
    assert caught.value.code == "invalid_config"
    assert "turns compaction off" in str(caught.value)


class Note(BaseModel):
    text: str


def _writing() -> "Agent[None, str]":
    async def write(_args: Note, _ctx: RunContext[None]) -> str:
        return "written"

    writer = tool(name="write", description="Write.", input=Note, runs="host", execute=write)
    return agent(name="billing", model=scripted_model({"responses": []}), tools=[writer])


def test_a_host_member_with_an_effectful_tool_and_no_approvers_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        serving({"billing": _writing()}, {"billing": HostMemberOptions()})
    assert caught.value.code == "invalid_config"
    assert str(caught.value).endswith(
        "a host member is shared by every conversation in the tenant, so its approvals need"
        " approvers; add approvers to agent 'billing'"
    )


def test_within_ms_below_a_second_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        serving({"billing": billing()}, {"billing": HostMemberOptions(within_ms=999)})
    assert caught.value.code == "invalid_config"
    assert "members.billing.within_ms is 999" in str(caught.value)


# Both options are the shared PosInt check, so this is the table TypeScript's
# host-members.test.ts runs: True is an int subclass in Python, and past MAX_SAFE_INTEGER the wire
# can't hold the value, which is where unbounded Python ints and JavaScript numbers drift apart.
_NOT_POS_INT: list[object] = [0, -1, 1.5, 2**53, True, "3"]


@pytest.mark.parametrize("field", ["max_restarts", "within_ms"])
@pytest.mark.parametrize("value", _NOT_POS_INT)
def test_a_member_option_takes_the_pos_int_check(field: str, value: object) -> None:
    options = HostMemberOptions(**{field: value})  # pyright: ignore[reportArgumentType]
    with pytest.raises(ConfigError) as caught:
        serving({"billing": billing()}, {"billing": options})
    assert caught.value.code == "invalid_config"
    assert f"members.billing.{field} is {value!r}" in str(caught.value)


@pytest.mark.parametrize("op", ["start", "monitor", "cancel"])
def test_a_rule_to_a_host_member_allowing_start_monitor_or_cancel_is_refused(op: str) -> None:
    agents = {"support": support([]), "billing": billing()}
    with pytest.raises(ConfigError) as caught:
        serving(agents, {"billing": HostMemberOptions()}, [rule("support", "billing", [op])])
    assert caught.value.code == "invalid_config"
    assert "billing is a host member, which the host starts, watches and stops" in str(caught.value)
    assert f"drop {op} from its allow" in str(caught.value)


@pytest.mark.parametrize("op", ["start", "monitor", "cancel"])
def test_a_rule_from_a_host_member_allowing_start_monitor_or_cancel_is_refused(op: str) -> None:
    """A host member leads no team. Section C makes any `from` with a start rule a lead, so
    without this billing would be a host member and a lead at once — and its own pinned `team`,
    which setup does refuse, could not catch it, because the leadership comes from the rule."""
    agents = {"support": support([]), "billing": billing()}
    with pytest.raises(ConfigError) as caught:
        serving(agents, {"billing": HostMemberOptions()}, [rule("billing", "support", [op])])
    assert caught.value.code == "invalid_config"
    assert "billing is a host member, which leads no team" in str(caught.value)
    assert f"drop {op} from its allow" in str(caught.value)


@pytest.mark.parametrize("allow", [["send"], ["ask"], ["send", "ask"]])
def test_a_rule_around_a_host_member_may_allow_send_and_ask(allow: list[str]) -> None:
    agents = {"support": support([]), "billing": billing()}
    members = {"billing": HostMemberOptions()}
    serving(agents, members, [rule("support", "billing", allow)])
    serving(agents, members, [rule("billing", "support", allow)])


def test_a_rule_between_two_host_members_allowing_anything_but_send_or_ask_is_refused() -> None:
    hr = agent(name="hr", model=scripted_model({"responses": []}))
    agents = {"hr": hr, "billing": billing()}
    members = {"hr": HostMemberOptions(), "billing": HostMemberOptions()}
    with pytest.raises(ConfigError) as caught:
        serving(agents, members, [rule("billing", "hr", ["ask", "monitor"])])
    assert caught.value.code == "invalid_config"
    assert "one host member may only send to and ask another" in str(caught.value)
    assert "drop monitor from its allow" in str(caught.value)

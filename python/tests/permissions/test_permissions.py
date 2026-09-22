"""The permission engine beyond the corpus: the rule grammar, bad input, thread rules, and a
property test that an unparseable shell construct is never allowed by a rule."""

from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from threads.log import PermissionDecisionData, PermissionRuleAddedData, Permissions
from threads.permissions import Call, Source, Verdict, decide, parse_rule
from threads.result import Err, Ok

WORKSPACE = "/workspace"
CHALLENGE = "0192c000-0000-7000-8000-000000000001"


def perms(allow: list[str], deny: list[str] | None = None) -> Permissions:
    return Permissions.model_validate(
        {
            "mode": "default",
            "allow": allow,
            "ask": [],
            "deny": deny or [],
            "protected_paths": [".git/**", "**/.ssh/**"],
            "allow_bypass": False,
            "plan_exit_mode": "default",
        }
    )


def thread_rule(rule: str) -> PermissionRuleAddedData:
    data = {"challenge_id": CHALLENGE, "decision": "allow", "rule": rule}
    return PermissionRuleAddedData.model_validate(data)


def test_literals_match_the_schema() -> None:
    fields = PermissionDecisionData.model_fields
    assert set(get_args(Source.__value__)) == set(get_args(fields["source"].annotation))
    assert set(get_args(Verdict.__value__)) == set(get_args(fields["decision"].annotation))


@pytest.mark.parametrize(
    "text",
    [
        "Bash(ls)",
        "bash()",
        "mcp__github__*(x)",
        "bash($(rm -rf /))",
        "bash(ls; rm x)",
        "web_fetch(example.com)",
        "send_email(bob)",
        "",
    ],
)
def test_invalid_rules_are_rejected(text: str) -> None:
    assert isinstance(parse_rule(text), Err)


@pytest.mark.parametrize(
    "text",
    ["bash", "bash(git status:*)", "edit(src/**)", "mcp__github__*", "web_fetch(domain:*.x.io)"],
)
def test_valid_rules_parse(text: str) -> None:
    assert isinstance(parse_rule(text), Ok)


@pytest.mark.parametrize("bad", [None, [1, 2], "ls", {"command": 3}])
def test_bash_without_a_command_string_is_never_allowed(bad: JsonValue) -> None:
    got = decide(perms(["bash"]), WORKSPACE, "default", Call("bash", "other", bad))
    assert got.decision == "ask"


def test_thread_rules_follow_policy_rules() -> None:
    call = Call("bash", "other", {"command": "make x"})
    got = decide(perms([]), WORKSPACE, "default", call, thread_rules=[thread_rule("bash(make:*)")])
    assert (got.decision, got.source, got.rule) == ("allow", "thread_rule", "bash(make:*)")


def test_thread_rules_never_open_a_protected_path() -> None:
    call = Call("edit", "edit", {"path": ".git/config"})
    got = decide(perms([]), WORKSPACE, "default", call, thread_rules=[thread_rule("edit")])
    assert (got.decision, got.source) == ("ask", "protected_path")


CONSTRUCTS = [
    "$(rm x)",
    "`id`",
    "cat <(ls)",
    "(cd /)",
    "{ ls; }",
    "cat <<EOF",
    "eval ls",
    "exec ls",
    "f() { ls; }",
    "echo 'unterminated",
    'echo "$(id)"',
]
ALLOW_ALL = ["bash", "bash(git:*)", "bash(eval:*)", "bash(exec:*)", "bash(cat:*)", "bash(echo:*)"]
WORDS = st.text(alphabet="abcdefgitsu -./=", max_size=20)


@given(before=WORDS, construct=st.sampled_from(CONSTRUCTS), after=WORDS)
def test_unparseable_is_never_allowed_by_a_rule(before: str, construct: str, after: str) -> None:
    command = f"{before}; {construct} {after}"
    call = Call("bash", "other", {"command": command})
    got = decide(perms(ALLOW_ALL), WORKSPACE, "default", call)
    assert not (got.decision == "allow" and got.source in ("policy", "thread_rule")), command

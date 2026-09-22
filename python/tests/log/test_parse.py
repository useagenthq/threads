"""parse_log_line at the storage boundary: invalid input of every kind, and the critical rule."""

import json

import pytest
from pydantic import JsonValue

from threads.log import (
    Head,
    Header,
    ParseError,
    UnknownEvent,
    UserInputEvent,
    parse_log_line,
)
from threads.log.parse import MAX_LINE_BYTES
from threads.result import Err, Ok

THREAD = "0192a000-0000-7000-8000-000000000001"
BRANCH = "0192b000-0000-7000-8000-000000000001"
HASH = "0" * 64
PRINCIPAL: JsonValue = {"issuer": "api", "tenant": "acme", "subject": "alice"}
USER: JsonValue = {"kind": "user", "principal": PRINCIPAL}


def event(kind: str, data: JsonValue, **envelope: JsonValue) -> dict[str, JsonValue]:
    line: dict[str, JsonValue] = {
        "seq": 1,
        "event_id": "0192e000-0000-7000-8000-000000000001",
        "thread_id": THREAD,
        "branch_id": BRANCH,
        "epoch": 1,
        "type": kind,
        "type_version": 1,
        "time": 0,
        "actor": {"kind": "host"},
        "prev_hash": HASH,
        "critical": True,
        "data": data,
    }
    return line | envelope


def parse(value: JsonValue) -> Ok[object] | Err[ParseError]:
    match parse_log_line(json.dumps(value)):
        case Ok(value=parsed):
            return Ok(parsed)
        case Err() as err:
            return err


def error_code(value: JsonValue) -> str:
    result = parse(value)
    assert isinstance(result, Err), result
    return result.error.code


USER_INPUT = event("user_input", {"source": "api", "text": "hi"}, actor=USER)
TOOL_RESULT: dict[str, JsonValue] = {
    "call_id": "c1",
    "is_error": False,
    "completeness": "complete",
    "preview": "ok",
}
FORK: dict[str, JsonValue] = {
    "parent_branch_id": BRANCH,
    "at_hash": HASH,
    "reason": "snapshot",
    "sandbox_id": "sb",
    "knowledge_policy": "pinned",
}
RESULT_REF: dict[str, JsonValue] = {"sha256": HASH, "bytes": 1, "media_type": "text/plain"}
NO_SANDBOX: dict[str, JsonValue] = {
    k: v for k, v in FORK.items() if k not in ("sandbox_id", "knowledge_policy")
}
RESOLVED: dict[str, JsonValue] = {
    "call_id": "c1",
    "outcome": "confirmed_success",
    "result_ref": RESULT_REF,
}


def test_header_head_and_event_parse_to_their_models() -> None:
    header: dict[str, JsonValue] = {
        "format": "threads.log",
        "format_version": 1,
        "thread_id": THREAD,
        "branch_id": BRANCH,
        "created_at": 0,
        "writer": {"impl": "threads-py", "version": "0.0.0"},
    }
    head: dict[str, JsonValue] = {
        "format": "threads.head",
        "format_version": 1,
        "branch_id": BRANCH,
        "seq": 0,
    }
    parsed_header = parse(header)
    assert isinstance(parsed_header, Ok)
    assert isinstance(parsed_header.value, Header)
    parsed_head = parse({**head, "hash": HASH})
    assert isinstance(parsed_head, Ok)
    assert isinstance(parsed_head.value, Head)
    parsed_event = parse(USER_INPUT)
    assert isinstance(parsed_event, Ok)
    assert isinstance(parsed_event.value, UserInputEvent)


@pytest.mark.parametrize(
    "line",
    [
        '{"a":1,"a":2}',
        '{"a":1,"\\u0061":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":-Infinity}',
        '{"x":1e400}',
        '{"x":"\\ud800"}',
        '{"\\udfff":1}',
        '{"x":9007199254740992}',
        '{"x":1e300}',
        '{"x":1',
        "[]",
        "",
    ],
)
def test_inadmissible_json_is_an_invalid_line(line: str) -> None:
    result = parse_log_line(line)
    assert isinstance(result, Err)
    assert result.error.code == "invalid_line"


def test_oversized_line_is_invalid() -> None:
    padded = event("user_input", {"source": "api", "text": "x" * MAX_LINE_BYTES}, actor=USER)
    assert error_code(padded) == "invalid_line"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(USER_INPUT | {"seq": "1"}, id="string for int (strict)"),
        pytest.param(USER_INPUT | {"seq": 1.0}, id="float for int"),
        pytest.param(USER_INPUT | {"seq": 0}, id="seq below minimum"),
        pytest.param(USER_INPUT | {"event_id": "not-a-uuid"}, id="id pattern"),
        pytest.param(USER_INPUT | {"critical": False}, id="pinned critical"),
        pytest.param(USER_INPUT | {"critical": 1}, id="1 for literal true"),
        pytest.param(USER_INPUT | {"type_version": True}, id="true for literal 1"),
        pytest.param(USER_INPUT | {"type_version": 1.0}, id="1.0 for literal 1"),
        pytest.param(USER_INPUT | {"actor": {"kind": "user"}}, id="user_input needs principal"),
        pytest.param(USER_INPUT | {"extra": 1}, id="unknown envelope key"),
        pytest.param(
            event("user_input", {"source": "api", "text": "hi", "x": 1}, actor=USER),
            id="unknown data key",
        ),
        pytest.param(
            event("user_input", {"source": "api", "text": "hi", "budget": None}, actor=USER),
            id="null for an optional field",
        ),
        pytest.param(
            event(
                "user_input",
                {"source": "api", "text": "hi", "content": [{"type": "text", "text": "hi"}]},
                actor=USER,
            ),
            id="text and content",
        ),
        pytest.param(
            event("user_input", {"source": "api"}, actor=USER), id="neither text nor content"
        ),
        pytest.param(
            event("user_input", {"source": "api", "text": "hi", "budget": {}}, actor=USER),
            id="empty budget (minProperties)",
        ),
        pytest.param(
            event("user_input", {"source": "api", "content": []}, actor=USER), id="empty content"
        ),
        pytest.param(event("fork", FORK | {"sandbox_id": None}), id="null sandbox_id"),
        pytest.param(event("fork", NO_SANDBOX), id="snapshot fork without sandbox"),
        pytest.param(event("fork", FORK | {"reason": "repair"}), id="repair fork with sandbox"),
        pytest.param(
            event("tool_result", TOOL_RESULT | {"origin": "answered"}),
            id="answered result without principal",
        ),
        pytest.param(
            event("effect_resolved", RESOLVED | {"by": "human"}),
            id="effect resolved by the wrong party",
        ),
        pytest.param(event("telemetry_ping", {}, seq=0, critical=False), id="unknown bad envelope"),
    ],
)
def test_schema_violation_is_an_invalid_line(value: JsonValue) -> None:
    assert error_code(value) == "invalid_line"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(event("fork", FORK), id="snapshot fork"),
        pytest.param(event("fork", NO_SANDBOX | {"reason": "repair"}), id="repair fork"),
        pytest.param(
            event("tool_result", TOOL_RESULT | {"origin": "answered"}, actor=USER),
            id="answered result with principal",
        ),
        pytest.param(event("effect_resolved", RESOLVED | {"by": "adapter"}), id="resolved"),
        pytest.param(
            event(
                "user_input",
                {"source": "api", "text": "hi", "budget": {"max_turns": 1}},
                actor=USER,
            ),
            id="budget",
        ),
        pytest.param(
            event(
                "user_input",
                {"source": "api", "content": [{"type": "text", "text": "hi"}]},
                actor=USER,
            ),
            id="content",
        ),
    ],
)
def test_conditional_rules_accept_the_valid_branch(value: JsonValue) -> None:
    assert isinstance(parse(value), Ok)


def test_unknown_critical_event_refuses() -> None:
    assert error_code(event("approval_quorum", {"n": 2})) == "unsupported_critical_event"


def test_newer_version_of_a_known_type_follows_the_critical_rule() -> None:
    newer = USER_INPUT | {"type_version": 2}
    assert error_code(newer) == "unsupported_critical_event"
    kept = parse(newer | {"critical": False})
    assert isinstance(kept, Ok)
    assert isinstance(kept.value, UnknownEvent)


def test_unknown_non_critical_event_is_kept_with_its_data() -> None:
    result = parse(event("telemetry_ping", {"cpu": 0.5, "tags": ["a"]}, critical=False))
    assert isinstance(result, Ok)
    assert isinstance(result.value, UnknownEvent)
    assert result.value.data == {"cpu": 0.5, "tags": ["a"]}


HEADER: dict[str, JsonValue] = {
    "format": "threads.log",
    "format_version": 1,
    "thread_id": THREAD,
    "branch_id": BRANCH,
    "created_at": 0,
    "writer": {"impl": "threads-py", "version": "0.0.0"},
}
HEAD: dict[str, JsonValue] = {
    "format": "threads.head",
    "format_version": 1,
    "branch_id": BRANCH,
    "seq": 0,
    "hash": HASH,
}


@pytest.mark.parametrize(
    ("value", "code"),
    [
        pytest.param(HEADER | {"format_version": 2}, "unsupported_format", id="header v2"),
        pytest.param(HEAD | {"format_version": 7}, "unsupported_format", id="head v7"),
        pytest.param(HEADER | {"format_version": 1.5}, "invalid_line", id="fraction"),
        pytest.param(HEADER | {"format_version": 2.0}, "invalid_line", id="float two"),
        pytest.param(HEADER | {"format_version": "2"}, "invalid_line", id="string"),
        pytest.param(HEADER | {"format_version": True}, "invalid_line", id="bool"),
        pytest.param(HEADER | {"format_version": 1.0}, "invalid_line", id="float one"),
        pytest.param(HEADER | {"format_version": 0}, "invalid_line", id="zero"),
        pytest.param(HEADER | {"format_version": -1}, "invalid_line", id="negative"),
        pytest.param(
            {k: v for k, v in HEADER.items() if k != "format_version"}, "invalid_line", id="missing"
        ),
        pytest.param(
            HEADER | {"format": "threads.lag", "format_version": 2},
            "invalid_line",
            id="unknown format",
        ),
        pytest.param(
            HEADER | {"format": 1, "format_version": 2}, "invalid_line", id="format not a string"
        ),
    ],
)
def test_format_admission(value: JsonValue, code: str) -> None:
    assert error_code(value) == code

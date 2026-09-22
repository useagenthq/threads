"""validate_next rules the conformance corpus doesn't reach, checked through the writer."""

import asyncio
from collections.abc import Sequence

import pytest
from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import Draft, SqliteStore

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
NOW = 1_790_000_000_000
REQUEST = "0192e000-0000-7000-8000-000000000099"
CHALLENGE = "0192c000-0000-7000-8000-000000000001"
ALICE: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "acme", "subject": "alice"},
}
EMAIL: dict[str, JsonValue] = {
    "name": "send_email",
    "description": "Send an email.",
    "input_schema": {"type": "object"},
    "effect_class": "unguarded",
}
READ: dict[str, JsonValue] = {**EMAIL, "name": "read_file", "effect_class": "read_only"}
STARTED = Draft(
    "thread_started",
    {
        "agent_name": "demo",
        "config_hash": "0" * 64,
        "instructions": "",
        "model": {"provider": "scripted", "name": "scripted-1"},
        "model_params": {},
        "adapter": {"name": "scripted", "version": "1", "settings": {}},
        "tools": [EMAIL, READ],
    },
)


def call(name: str, call_id: str = "call_1") -> Draft:
    data: dict[str, JsonValue] = {
        "call_id": call_id,
        "name": name,
        "input": {},
        "request_event_id": REQUEST,
    }
    return Draft("tool_call", data)


def allow(call_id: str = "call_1") -> Draft:
    return Draft(
        "permission_decision", {"call_id": call_id, "decision": "allow", "source": "policy"}
    )


def result(text: str, call_id: str = "call_1") -> Draft:
    data: dict[str, JsonValue] = {
        "call_id": call_id,
        "is_error": False,
        "completeness": "complete",
        "preview": text,
        "origin": "executed",
    }
    return Draft("tool_result", data, actor={"kind": "tool"})


def redact(start: int, end: int) -> Draft:
    edit: dict[str, JsonValue] = {
        "call_id": "call_1",
        "action": "redact",
        "part": 0,
        "spans": [{"start": start, "end": end}],
    }
    return Draft("context_edited", {"reason": "guardrail", "edits": [edit]})


def approval(kind: str, args_hash: str = "a" * 64) -> Draft:
    data: dict[str, JsonValue] = {
        "challenge_id": CHALLENGE,
        "call_id": "call_1",
        "args_hash": args_hash,
    }
    return Draft(kind, data, actor={**ALICE, "kind": "approver"})


def delivery(item: str) -> Draft:
    data: dict[str, JsonValue] = {
        "channel": "slack",
        "installation": "T1",
        "conversation": "C1",
        "delivery_id": "d1",
        "item_key": item,
        "text": "hi",
    }
    return Draft("channel_delivery", data, actor={**ALICE, "kind": "channel"})


REQUESTED = Draft(
    "approval_requested",
    {"challenge_id": CHALLENGE, "call_id": "call_1", "args_hash": "a" * 64, "expires_at": NOW},
)
BEGIN = Draft("effect_begin", {"call_id": "call_1", "attempt": 1})
CANCEL = Draft("cancel_requested", {"scope": "turn"}, actor=ALICE)


async def last_code(drafts: Sequence[Draft]) -> str | None:
    """Appends drafts one by one; every draft but the last must be accepted."""
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        assert await store.create(THREAD, ROOT, NOW) == Ok(None)
        writer = await store.acquire(ROOT, "a", lambda: NOW)
        assert isinstance(writer, Ok)
        *before, last = [STARTED, *drafts]
        for draft in before:
            assert isinstance(await writer.value.append([draft]), Ok), draft
        appended = await writer.value.append([last])
    finally:
        await store.close()
    return appended.error.code if isinstance(appended, Err) else None


@pytest.mark.parametrize(
    ("drafts", "code"),
    [
        # Rule 19: spans are UTF-8 byte offsets on character boundaries, inside the part.
        ([call("send_email"), result("héllo"), redact(0, 1)], None),
        ([call("send_email"), result("héllo"), redact(0, 2)], "invalid_transition"),
        ([call("send_email"), result("héllo"), redact(0, 7)], "invalid_transition"),
        # Rule 9: a channel item key is used once per branch.
        ([delivery("i1"), delivery("i2")], None),
        ([delivery("i1"), delivery("i1")], "invalid_transition"),
        # Rule 13 and rule 9: a challenge is answered once, by a matching binding.
        ([call("send_email"), REQUESTED, approval("approval_granted")], None),
        (
            [call("send_email"), REQUESTED, approval("approval_granted", "b" * 64)],
            "approval_mismatch",
        ),
        (
            [
                call("send_email"),
                REQUESTED,
                approval("approval_granted"),
                approval("approval_denied"),
            ],
            "invalid_transition",
        ),
        # Rule 8: a consumed approval allows the effect; a cancel barrier after the call doesn't.
        ([call("send_email"), REQUESTED, approval("approval_granted"), BEGIN], None),
        ([call("send_email"), allow(), CANCEL, BEGIN], "invalid_transition"),
        ([call("read_file"), allow(), BEGIN], "invalid_transition"),
    ],
)
def test_rule(drafts: list[Draft], code: str | None) -> None:
    assert asyncio.run(last_code(drafts)) == code


async def reacquired(drafts: Sequence[Draft]) -> bool:
    """Appends drafts, then takes the branch again as a restarted runner would."""
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        assert await store.create(THREAD, ROOT, NOW) == Ok(None)
        writer = await store.acquire(ROOT, "a", lambda: NOW)
        assert isinstance(writer, Ok)
        assert isinstance(await writer.value.append([STARTED, *drafts]), Ok)
        again = await store.acquire(ROOT, "a", lambda: NOW)
        assert isinstance(again, Ok)
        return again.value.requires_recovery
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("drafts", "needed"),
    [
        ([], False),
        ([call("read_file"), allow(), result("ok")], False),
        ([call("read_file")], True),
        ([call("send_email"), allow(), BEGIN], True),
        ([call("send_email"), allow(), BEGIN, result("sent")], False),
    ],
)
def test_acquire_flags_unsettled_work_for_recovery(drafts: list[Draft], needed: bool) -> None:
    assert asyncio.run(reacquired(drafts)) is needed

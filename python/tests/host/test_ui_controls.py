"""The UI routes' human input and bookkeeping (spec/schema/ui/README.md): approval authority is
unchanged, an assistant message with nothing new takes no receipt, an import rebuilds the `ui`
receipts, and the interrupt answer schemas are host-api's. The registration and head-read race
of live text is the `ui-live-race-*` cases (test_ui_cases)."""

import asyncio
import json
import sqlite3
from http import HTTPStatus
from pathlib import Path

from corpus import CASES, stored_artifacts
from host.test_http import run, sender, served
from host.ui_kit import AI_SDK, chat, frames, post, user
from pydantic import JsonValue

from threads import sqlite
from threads.host.ui.interrupts import ANSWER_SCHEMA, APPROVAL_DECISION_SCHEMA
from threads.log import ThreadId
from threads.result import Ok
from threads.store import SqliteStore, verify_export

HOST_API = Path(__file__).resolve().parents[3] / "spec" / "schema" / "host-api"


def test_the_interrupt_answer_schemas_are_host_apis() -> None:
    defs = json.loads((HOST_API / "host-api.v1.schema.json").read_bytes())["$defs"]
    assert defs["ApprovalDecision"] == APPROVAL_DECISION_SCHEMA
    assert defs["Answer"] == ANSWER_SCHEMA


def approval_of(first: list[tuple[str | None, JsonValue]]) -> str:
    found = next(
        d for _, d in first if isinstance(d, dict) and d.get("type") == "tool-approval-request"
    )
    assert isinstance(found, dict)
    return str(found["approvalId"])


def responded(approval: str, approved: bool) -> JsonValue:
    part: JsonValue = {
        "type": "tool-send",
        "toolCallId": "call_1",
        "state": "approval-responded",
        "approval": {"id": approval, "approved": approved},
    }
    return {"id": "a1", "role": "assistant", "parts": [part]}


def test_approval_authority_is_unchanged_and_a_repeat_decision_is_a_no_op(tmp_path: Path) -> None:
    async def main() -> None:
        # The sender agent's approvers are Alice only: Bob's own chat can't approve.
        async with served(sender([]), store=sqlite(str(tmp_path))) as client:
            first = await post(client, AI_SDK, "bob", chat("c", user("m1", "Send x")))
            approval = approval_of(frames(first))
            asked = responded(approval, True)
            refused = await post(client, AI_SDK, "bob", chat("c", user("m1", "Send x"), asked))
            assert refused.status_code == HTTPStatus.FORBIDDEN
            assert refused.json()["error"]["code"] == "forbidden"
            again = await post(client, AI_SDK, "bob", chat("c", user("m1", "Send x"), asked))
            assert again.status_code == HTTPStatus.FORBIDDEN

    run(main)


def test_an_assistant_message_with_nothing_new_is_204_and_takes_no_receipt(
    tmp_path: Path,
) -> None:
    async def main() -> None:
        async with served(sender([]), store=sqlite(str(tmp_path))) as client:
            await post(client, AI_SDK, "alice", chat("c", user("m1", "Send x")))
            nothing: JsonValue = {"id": "a1", "role": "assistant", "parts": []}
            answered = await post(client, AI_SDK, "alice", chat("c", user("m1", "x"), nothing))
            assert answered.status_code == HTTPStatus.NO_CONTENT

    run(main)
    with sqlite3.connect(tmp_path / "threads.db") as conn:
        rows = conn.execute("SELECT operation, count(*) FROM run_receipts GROUP BY 1").fetchall()
    assert rows == [("ui", 1)]


def test_an_import_rebuilds_the_ui_receipts() -> None:
    case = CASES / "ui-snapshot-client-ids-ag-ui"

    async def main() -> dict[str, str]:
        verified = verify_export((case / "log.jsonl").read_bytes(), 1_790_000_060_000)
        assert isinstance(verified, Ok)
        opened = await SqliteStore.open(tenant_id="acme", artifacts=stored_artifacts(case))
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert isinstance(await store.import_log(verified.value), Ok)
            thread = ThreadId("0192a000-0000-7000-8000-000000000001")
            return await store.tables.ui_messages(thread)
        finally:
            await store.close()

    assert asyncio.run(main()) == {"0192e000-0000-7000-8000-000000000002": "msg-abc"}

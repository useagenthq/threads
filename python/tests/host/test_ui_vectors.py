"""The shared UI vectors (spec/conformance/vectors): chat keys to thread ids, the fold the stock
AG-UI client (@ag-ui/client@1.0.0) applies, and each UI request body accepted or refused with its
code before any stream, appending nothing."""

import sqlite3
from http import HTTPStatus
from pathlib import Path

import pytest
from host.test_http import as_, run, sender, served
from pydantic import JsonValue, TypeAdapter

from threads import sqlite
from threads.host.ui.ag_ui_fold import AgUiFold
from threads.host.ui.key import CHAT_KEY, ui_thread_id
from threads.log import Principal

VECTORS = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors"
_LIST: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(list[dict[str, JsonValue]])


def vector(name: str) -> list[dict[str, JsonValue]]:
    return _LIST.validate_json((VECTORS / name).read_bytes())


@pytest.mark.parametrize("entry", vector("ui-thread-ids.json"), ids=lambda e: str(e["name"]))
def test_a_chat_key_names_its_thread(entry: dict[str, JsonValue]) -> None:
    key = str(entry["key"])
    if "error" in entry:
        assert not CHAT_KEY.match(key)
        return
    principal = Principal.model_validate(entry["principal"])
    assert ui_thread_id(principal, str(entry["agent"]), key) == entry["thread_id"]


@pytest.mark.parametrize("entry", vector("ag-ui-fold.json"), ids=lambda e: str(e["name"]))
def test_the_fold_is_the_stock_clients(entry: dict[str, JsonValue]) -> None:
    initial = entry["initial"]
    events = entry["events"]
    assert isinstance(initial, list)
    assert isinstance(events, list)
    fold = AgUiFold([m for m in initial if isinstance(m, dict)])
    for e in events:
        assert isinstance(e, dict)
        fold.apply(e)
    keys = ("id", "role", "content", "toolCallId", "toolCalls")
    assert [{k: m[k] for k in keys if k in m} for m in fold.messages] == entry["messages"]


@pytest.mark.parametrize("entry", vector("ui-inputs.json"), ids=lambda e: str(e["name"]))
def test_a_ui_body_is_accepted_or_refused_before_any_stream(
    entry: dict[str, JsonValue], tmp_path: Path
) -> None:
    path = "/v1/ui/ai-sdk/support" if entry["protocol"] == "ai-sdk" else "/v1/ui/ag-ui/support"
    code = entry["code"]

    async def main() -> None:
        async with served(sender([]), store=sqlite(str(tmp_path))) as client:
            answered = await client.post(path, json=entry["body"], headers=as_("alice"))
            if code is None:
                assert answered.status_code == HTTPStatus.OK, answered.text
            else:
                assert answered.json()["error"]["code"] == code

    run(main)
    if code is not None:
        with sqlite3.connect(tmp_path / "threads.db") as conn:
            assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0

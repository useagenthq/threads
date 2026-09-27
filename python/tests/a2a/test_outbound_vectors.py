"""spec/conformance/vectors/a2a.json, `outbound`: the derivations a client must agree on with
itself across processes and with the other language.

The vector is generated from their definitions, not from either implementation's output, so a change
that drifts from the contract fails here rather than quietly re-pinning itself. TypeScript runs the
same rows in typescript/packages/a2a/test/outbound/vectors.test.ts."""

import pathlib
from typing import Final

import pytest
from pydantic import TypeAdapter

from threads.a2a.outbound.derive import context_id_of, message_id_of, request_id_of

VECTOR: Final = (
    pathlib.Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors" / "a2a.json"
)

_TABLES = TypeAdapter[dict[str, object]](dict[str, object])
_ROWS_OF = TypeAdapter[list[dict[str, str]]](list[dict[str, str]])
_ROWS: Final = _ROWS_OF.validate_python(_TABLES.validate_json(VECTOR.read_bytes())["outbound"])


def _row_id(row: dict[str, str]) -> str:
    return row["name"]


@pytest.mark.parametrize("row", _ROWS, ids=_row_id)
def test_the_outbound_derivations_match_the_pinned_vector(row: dict[str, str]) -> None:
    # The messageId comes from (branch_id, call_id) alone, never from the attempt: that is what
    # makes every re-dispatch of one call look like one message to a peer that deduplicates.
    assert message_id_of(row["branch_id"], row["call_id"]) == row["message_id"]
    assert context_id_of(row["thread_id"], row["remote"]) == row["context_id"]
    assert request_id_of(row["tenant"], row["thread_id"]) == row["request"]


def test_an_attempt_never_changes_the_message_id() -> None:
    row = _ROWS[0]
    once = message_id_of(row["branch_id"], row["call_id"])
    assert once == message_id_of(row["branch_id"], row["call_id"])
    # A different call, and only a different call, is a different message.
    assert once != message_id_of(row["branch_id"], row["call_id"] + "x")

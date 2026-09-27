"""spec/conformance/vectors/a2a.json, `keys`: the derivations two hosts on one store must agree on,
whichever language each is written in. The vector is generated from their definitions, not from
either implementation's output, so a change that drifts from the contract fails here rather than
quietly re-pinning itself. TypeScript runs the same rows in
typescript/packages/host/test/a2a/keys.test.ts."""

import pathlib
from typing import Final

import pytest
from pydantic import TypeAdapter

from threadsai.host.a2a.keys import a2a_thread_id, body_hash, send_key
from threadsai.host.ui.key import ui_thread_id
from threadsai.log import Principal

VECTOR: Final = (
    pathlib.Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors" / "a2a.json"
)


_TABLES = TypeAdapter[dict[str, object]](dict[str, object])
_ROWS_OF = TypeAdapter[list[dict[str, str]]](list[dict[str, str]])


def _keys() -> list[dict[str, str]]:
    """The vector's `keys` table, typed: every field of a row is a string. The other tables belong
    to the protocol core's own suite, so only this one is read here."""
    return _ROWS_OF.validate_python(_TABLES.validate_json(VECTOR.read_bytes())["keys"])


_ROWS: Final = _keys()

QUESTION: Final = "Where is order 1042?"
"""The text the vector's rows hash; the generator writes the same message."""


def _row_id(row: dict[str, str]) -> str:
    return row["name"]


@pytest.mark.parametrize("row", _ROWS, ids=_row_id)
def test_the_derivations_match_the_pinned_vector(row: dict[str, str]) -> None:
    principal = Principal(issuer=row["issuer"], tenant=row["tenant"], subject=row["subject"])
    assert send_key(principal, row["agent"], row["message_id"]) == row["send_key"]
    assert a2a_thread_id(principal, row["agent"], row["context_id"]) == row["thread_id"]
    assert (
        body_hash(
            row["agent"],
            {
                "messageId": row["message_id"],
                "role": "ROLE_USER",
                "parts": [{"text": QUESTION}],
            },
        )
        == row["body_hash"]
    )


def test_a_context_never_names_the_thread_a_browser_chat_would() -> None:
    # The two derivations differ only by their domain string, which is the whole point of having
    # one: a partner must not be able to reach a signed-in person's chat by guessing a key.
    principal = Principal(issuer="api", tenant="acme", subject="a@acme.example")
    assert a2a_thread_id(principal, "support", "shared") != ui_thread_id(
        principal, "support", "shared"
    )


def test_two_principals_equal_message_ids_never_share_a_receipt_key() -> None:
    one = Principal(issuer="api", tenant="acme", subject="a")
    two = Principal(issuer="api", tenant="acme", subject="b")
    assert send_key(one, "support", "m-1") != send_key(two, "support", "m-1")


def test_a_crafted_principal_part_cannot_forge_another_principals_key() -> None:
    # principal_key escapes % and / per part, and each field is length-prefixed, so no crafted
    # subject can make one principal's key read as another's.
    crafted = Principal(issuer="api", tenant="acme", subject="b/support/m-1")
    plain = Principal(issuer="api", tenant="acme", subject="b")
    assert send_key(crafted, "support", "m-1") != send_key(plain, "support", "m-1")

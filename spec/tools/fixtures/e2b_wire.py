# pyright: strict
"""The E2B wire vector (spec/conformance/vectors/e2b-wire/cases.json): each call both E2B
adapters make to E2B's REST API and to a sandbox's envd, the exact request each must send, a
provider answer replayed byte for byte, and what each adapter must make of it.

Sources: e2b-dev/E2B at ccaf9fc0ffe6ac39c7ec786af7608ab1de19467b (tags e2b@2.51.0 and
@e2b/python-sdk@2.51.0, the SDK Python pins): `spec/openapi.yml` (`POST /v2/sandboxes`,
`GET /v2/sandboxes`, `GET`/`DELETE /sandboxes/{id}`), `spec/envd/process/process.proto` (the
`process.Process` messages) and `spec/envd/envd.yaml` (`/files`). Envd's process calls use
Connect with its JSON codec: `Start` is a server stream of enveloped messages, `SendSignal` is
unary JSON with the error in the HTTP status. Requests and answers were checked against
Python's adapter, which sends through that SDK's generated clients."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import CASES
from .e2b_wire_envd import ENVD, file_cases, signal_cases, start_cases
from .e2b_wire_pieces import API_KEY, CAP, DOMAIN, KEY, SBX, sandbox
from .e2b_wire_rest import by_id_cases, create_cases, find_cases
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import Obj

VECTOR = CASES.parent / "vectors" / "e2b-wire" / "cases.json"


def _vector() -> str:
    doc = (
        "E2B's wire, shared by both E2B adapters (spec/tools/fixtures/e2b_wire.py cites the "
        "sources). Each case runs one call against the replayed exchanges in order: the adapter "
        "must send each request exactly (method, URL, the listed headers, the body as JSON, one "
        "Connect envelope or one multipart file part), must send nothing more, and must end "
        "with `expect`: `ok` and its value, or `error` and the sandbox error code. No envd "
        "request may carry `api_key`. `envd_urls` is how an adapter reaches a sandbox's envd."
    )
    vector: Obj = {
        "_doc": doc,
        "api_key": API_KEY,
        "domain": DOMAIN,
        "envd_sandbox": sandbox(SBX, KEY),
        "envd_urls": [
            {"sandbox_id": SBX, "domain": DOMAIN, "url": ENVD},
            {"sandbox_id": SBX, "domain": "e2b.app", "url": "https://sandbox.e2b.app"},
        ],
        "max_message_bytes": CAP,
        "cases": [
            *create_cases(),
            *find_cases(),
            *by_id_cases(),
            *start_cases(),
            *signal_cases(),
            *file_cases(),
        ],
    }
    return dump(vector)


def write() -> None:
    VECTOR.parent.mkdir(exist_ok=True)
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    fresh = _vector()
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if fresh == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]

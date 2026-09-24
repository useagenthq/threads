# pyright: strict
"""The E2B wire vector's control-plane cases: create, the paginated list, describe and kill
(e2b_wire.py cites the sources)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .e2b_wire_pieces import (
    API,
    API_KEY,
    DOMAIN,
    KEY,
    KEY_NAME,
    SBX,
    TOKEN,
    case,
    empty,
    reply,
    sandbox,
)

if TYPE_CHECKING:
    from .jcs import Obj

LIST = f"{API}/v2/sandboxes?metadata={KEY_NAME}%3D{KEY}&state=running%2Cpaused"
REST_HEADERS: Obj = {"x-api-key": API_KEY}


def create_cases() -> list[Obj]:
    call: Obj = {
        "op": "create",
        "template": "base",
        "key": KEY,
        "timeout_s": 600,
        "internet": False,
    }
    body: Obj = {
        "templateID": "base",
        "timeout": 600,
        "autoPause": False,
        "autoPauseMemory": True,
        "allow_internet_access": False,
        "metadata": {KEY_NAME: KEY},
        "envVars": {},
    }
    req: Obj = {
        "method": "POST",
        "url": f"{API}/v2/sandboxes",
        "headers": {**REST_HEADERS, "content-type": "application/json"},
        "json": body,
    }
    made: Obj = {"sandbox_id": SBX, "envd_access_token": TOKEN, "domain": DOMAIN}
    wrong = {**sandbox(SBX, KEY), "sandboxID": 5}
    moved = empty(307, {"location": f"{API}/elsewhere"})
    return [
        case("create", call, req, reply(201, sandbox(SBX, KEY)), {"ok": made}),
        case(
            "create_unknown_template",
            call,
            req,
            reply(404, {"code": 404, "message": "no template"}),
            {"ok": None},
        ),
        case(
            "create_unexpected_status",
            call,
            req,
            reply(500, {"code": 500, "message": "boom"}),
            {"error": "unavailable"},
        ),
        case(
            "create_malformed_body",
            call,
            req,
            reply(201, {"unexpected": True}),
            {"error": "unavailable"},
        ),
        case("create_wrong_type", call, req, reply(201, wrong), {"error": "unavailable"}),
        case("create_redirect_not_followed", call, req, moved, {"error": "unavailable"}),
    ]


def _page(url: str, response: Obj) -> Obj:
    return {"request": {"method": "GET", "url": url, "headers": REST_HEADERS}, "response": response}


def find_cases() -> list[Obj]:
    call: Obj = {"op": "find", "key": KEY}
    first, second = f"{LIST}&limit=100", f"{LIST}&nextToken=page-2&limit=100"
    other = sandbox("sbx_other", "another-key")
    more: Obj = {"x-next-token": "page-2"}
    return [
        {
            "name": "find",
            "call": call,
            "exchanges": [_page(first, reply(200, [sandbox(SBX, KEY)]))],
            "expect": {"ok": [SBX]},
        },
        {
            "name": "find_follows_next_token",
            "call": call,
            "exchanges": [
                _page(first, reply(200, [other], more)),
                _page(second, reply(200, [sandbox(SBX, KEY)])),
            ],
            "expect": {"ok": [SBX]},
        },
        {
            "name": "find_page_failure_is_not_absence",
            "call": call,
            "exchanges": [
                _page(first, reply(200, [], more)),
                _page(second, reply(500, {"code": 500, "message": "boom"})),
            ],
            "expect": {"error": "unavailable"},
        },
        {
            "name": "find_none",
            "call": call,
            "exchanges": [_page(first, reply(200, [other]))],
            "expect": {"ok": []},
        },
        {
            "name": "find_malformed_body",
            "call": call,
            "exchanges": [_page(first, reply(200, {"not": "a list"}))],
            "expect": {"error": "unavailable"},
        },
    ]


def by_id_cases() -> list[Obj]:
    url = f"{API}/sandboxes/{SBX}"
    get: Obj = {"method": "GET", "url": url, "headers": REST_HEADERS}
    delete: Obj = {"method": "DELETE", "url": url, "headers": REST_HEADERS}
    describe: Obj = {"op": "describe", "id": SBX}
    kill: Obj = {"op": "kill", "id": SBX}
    made: Obj = {"sandbox_id": SBX, "envd_access_token": TOKEN, "domain": DOMAIN}
    missing = reply(404, {"code": 404, "message": "not found"})
    return [
        case("describe", describe, get, reply(200, sandbox(SBX, KEY)), {"ok": made}),
        case("describe_missing", describe, get, missing, {"ok": None}),
        case("describe_malformed_body", describe, get, reply(200, [1]), {"error": "unavailable"}),
        case("kill", kill, delete, empty(204), {"ok": True}),
        case("kill_already_gone", kill, delete, missing, {"ok": False}),
        case(
            "kill_unexpected_status",
            kill,
            delete,
            reply(500, {"code": 500, "message": "boom"}),
            {"error": "unavailable"},
        ),
    ]

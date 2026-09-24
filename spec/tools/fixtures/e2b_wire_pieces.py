# pyright: strict
"""The E2B wire vector's shared values and reply builders (e2b_wire.py)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

API_KEY = "e2b_vector_key_canary"
DOMAIN = "e2b.test"
API = f"https://api.{DOMAIN}"
KEY_NAME = "threads_operation_key"
KEY = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
SBX = "sbx_vector_1"
TOKEN = "envd-token-vector"  # noqa: S105 - a test vector's token, not a secret
CAP = 4 * 1024 * 1024
"""The largest envd message either adapter reads (4 MiB)."""


def sandbox(sandbox_id: str, key: str) -> Obj:
    """A sandbox as E2B describes it, with a field this vector's adapters don't know."""
    return {
        "templateID": "base",
        "sandboxID": sandbox_id,
        "clientID": "client",
        "envdVersion": "0.5.8",
        "envdAccessToken": TOKEN,
        "domain": DOMAIN,
        "startedAt": "2026-09-23T00:00:00Z",
        "endAt": "2026-09-23T01:00:00Z",
        "cpuCount": 2,
        "memoryMB": 512,
        "diskSizeMB": 1024,
        "state": "running",
        "metadata": {KEY_NAME: key},
        "futureField": {"added": "later"},
    }


def reply(status: int, body: JsonValue, headers: Obj | None = None) -> Obj:
    text = json.dumps(body, separators=(",", ":"))
    return {
        "status": status,
        "headers": {"content-type": "application/json", **(headers or {})},
        "text": text,
    }


def empty(status: int, headers: Obj | None = None) -> Obj:
    return {"status": status, "headers": headers or {}, "text": ""}


def case(name: str, call: Obj, request: Obj, response: Obj, expect: Obj) -> Obj:
    return {
        "name": name,
        "call": call,
        "exchanges": [{"request": request, "response": response}],
        "expect": expect,
    }

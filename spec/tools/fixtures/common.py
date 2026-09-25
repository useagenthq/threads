# pyright: strict
"""Constants, typed JSON access and hashing helpers."""

from __future__ import annotations

import hashlib
import pathlib

from .jcs import JsonValue, Obj

CASES = pathlib.Path(__file__).resolve().parents[2] / "conformance" / "cases"
STAGED = CASES.parent / "staged"
STAGED_PHASE_2_DIR = CASES.parent / "staged-phase-2"
THREAD = "0192a000-0000-7000-8000-000000000001"
BRANCH = "0192b000-0000-7000-8000-000000000001"
CHILD = "0192b000-0000-7000-8000-000000000002"
T0 = 1790000000000
NOW = T0 + 60_000
DAY = 86_400_000
MODEL: Obj = {"provider": "scripted", "name": "scripted-1"}
PARAMS: Obj = {"max_tokens": 1024}
ADAPTER: Obj = {"name": "scripted", "version": "1", "settings": {}}
ALICE: Obj = {"issuer": "api", "tenant": "acme", "subject": "alice"}
ALLOW: Obj = {"decision": "allow", "source": "policy", "rule_id": "conformance_allow"}


# ---------- typed access to parsed JSON ----------
def obj(v: JsonValue) -> Obj:
    if not isinstance(v, dict):
        raise TypeError(f"expected object, got {v!r}")
    return v


def text(v: JsonValue) -> str:
    if not isinstance(v, str):
        raise TypeError(f"expected string, got {v!r}")
    return v


def num(v: JsonValue) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"expected integer, got {v!r}")
    return v


def arr(v: JsonValue) -> list[JsonValue]:
    if not isinstance(v, list):
        raise TypeError(f"expected array, got {v!r}")
    return v


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def aref(b: bytes, mt: str) -> Obj:
    return {"sha256": sha(b), "bytes": len(b), "media_type": mt}


def eid(n: int, branch: str = BRANCH) -> str:
    # Distinct ids per branch keep event_id unique across the resolved chain.
    return f"0192e{0 if branch == BRANCH else 1:03x}-0000-7000-8000-{n:012x}"


def tool(name: str, desc: str, props: Obj, eclass: str, window: int | None = None) -> Obj:
    t: Obj = {
        "name": name,
        "description": desc,
        "effect_class": eclass,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": list[JsonValue](sorted(props)),
            "properties": props,
        },
    }
    if window is not None:
        t["dedup_window_ms"] = window
    return t


def tokens(i: int, o: int) -> Obj:
    return {"input_tokens": i, "output_tokens": o}


MAX_SAFE = 2**53 - 1
"""The wire integer range (Int): totals past it are never recorded."""

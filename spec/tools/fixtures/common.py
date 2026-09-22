# pyright: strict
"""Constants, typed JSON access, hashing helpers and the reference Render v1."""

from __future__ import annotations

import hashlib
import pathlib

from .jcs import JsonValue, Obj, canonical

CASES = pathlib.Path(__file__).resolve().parents[2] / "conformance" / "cases"
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


# ---------- Render v1 (normative text: spec/schema/README.md) ----------
def wrap(d: Obj, body: str) -> str:
    origin = text(obj(d["origin"])["id"])
    source = text(d["source"])
    if d["trust"] == "untrusted_reference":
        return f'<reference source="{source}" id="{origin}" untrusted="true">\n{body}\n</reference>'
    return f'<context source="{source}" id="{origin}">\n{body}\n</context>'


def user_line(t: str) -> Obj:
    return {"role": "user", "content": [{"type": "text", "text": t}]}


def render(events: list[Obj]) -> tuple[bytes, bytes]:
    """Returns (request bytes, declared prefix bytes = line 0)."""
    ts = obj(next(e for e in events if e["type"] == "thread_started")["data"])
    raw_tools = arr(ts["tools"])
    tools: list[JsonValue] = [
        {k: obj(t)[k] for k in ("name", "description", "input_schema")} for t in raw_tools
    ]
    line0 = (
        canonical(
            {
                "adapter": ts["adapter"],
                "model": ts["model"],
                "params": ts["model_params"],
                "system": ts["instructions"],
                "tools": tools,
            }
        )
        + b"\n"
    )
    out = [line0]
    for e in events:
        d, t = obj(e["data"]), text(e["type"])
        m: Obj
        if t in ("user_input", "steer"):
            m = user_line(text(d["text"]))
        elif t == "injected":
            m = user_line(wrap(d, text(d["text"])))
        elif t == "heartbeat":
            ids = arr(d["running_call_ids"])
            m = user_line(
                "<heartbeat>\nrunning: " + ", ".join(text(x) for x in ids) + "\n</heartbeat>"
            )
        elif t == "tools_changed":
            specs = arr(d["tools"])
            m = {
                "role": "tools",
                "tools": [
                    {k: obj(x)[k] for k in ("name", "description", "input_schema")} for x in specs
                ],
            }
        elif t in ("model_response", "model_response_recovered"):
            m = {"role": "assistant", "content": d["content"]}
        elif t in ("tool_result", "tool_result_late"):
            m = {
                "role": "tool",
                "call_id": d["call_id"],
                "is_error": d["is_error"],
                "content": [{"type": "text", "text": d["preview"]}],
            }
            if t == "tool_result_late":
                m["late"] = True
        elif t == "compacted":
            raise NotImplementedError("no fixture renders across compaction yet")
        else:
            continue
        out.append(canonical(m) + b"\n")
    return b"".join(out), line0

# pyright: strict
"""Reference projections beyond ReducedState (conformance README, "Projections")."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .log import Log

CAUSES = ("settings_changed", "compacted", "context_edited", "tools_changed")
USAGE_FIELDS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_tokens", "cache_read"),
    ("cache_write_tokens", "cache_write"),
)
DROP_MIN = 2000


def _policy(log: Log) -> Obj:
    ts = obj(next(e for e in log.events if e["type"] == "thread_started")["data"])
    return obj(ts.get("policy", {}))


def _requests(log: Log) -> dict[JsonValue, tuple[Obj, int]]:
    """model_request event_id -> (settings epoch at that request, request bytes)."""
    ts = obj(log.events[0]["data"])
    settings: Obj = {"model": ts["model"], "model_params": ts["model_params"]}
    out: dict[JsonValue, tuple[Obj, int]] = {}
    for e in log.events:
        d = obj(e["data"])
        if e["type"] == "settings_changed":
            settings = obj(d["settings"])
        elif e["type"] == "model_request":
            out[e["event_id"]] = (settings, num(obj(d["request_ref"])["bytes"]))
    return out


def cost(log: Log) -> Obj:
    """Known cost, and a conservative upper bound that charges unknown fields at their bound:
    request bytes for input and cache fields, the epoch's max_tokens for output."""
    pol = _policy(log)
    prices = {
        (text(obj(m)["provider"]), text(obj(m)["name"])): obj(obj(m)["price"])
        for m in arr(pol["models"])
    }
    requests = _requests(log)
    known = upper = 0
    complete = True
    for e in log.events:
        if e["type"] not in ("model_response", "model_response_recovered"):
            continue
        d = obj(e["data"])
        settings, req_bytes = requests[d["request_event_id"]]
        model = obj(settings["model"])
        price = prices[(text(model["provider"]), text(model["name"]))]
        usage = obj(d["usage"])
        for field, key in USAGE_FIELDS:
            if field not in usage:
                continue
            p = num(price.get(key, 0))
            v = usage[field]
            if v is None:
                complete = False
                bound = (
                    num(obj(settings["model_params"])["max_tokens"])
                    if key == "output"
                    else req_bytes
                )
                upper += bound * p
            else:
                known += num(v) * p
                upper += num(v) * p
    return {
        "currency": pol["currency"],
        "known_nanos": known,
        "upper_bound_nanos": upper,
        "complete": complete,
    }


def cache_breaks(log: Log) -> list[JsonValue]:
    """A drop of cache reads below 95% of the previous turn request's, by at least 2000 tokens."""
    ttl = num(obj(_policy(log)["context"])["cache_ttl_ms"])
    purpose = {
        e["event_id"]: obj(e["data"]).get("purpose", "turn")
        for e in log.events
        if e["type"] == "model_request"
    }
    request_time = {
        e["event_id"]: num(e["time"]) for e in log.events if e["type"] == "model_request"
    }
    out: list[JsonValue] = []
    prev: tuple[int, int] | None = None  # (cache reads, response time)
    seen: list[str] = []
    for e in log.events:
        t = text(e["type"])
        if t in CAUSES:
            seen.append(t)
            continue
        if t != "model_response" or purpose[obj(e["data"])["request_event_id"]] != "turn":
            continue
        d = obj(e["data"])
        cr = obj(d["usage"]).get("cache_read_tokens")
        if not isinstance(cr, int):
            continue
        if prev is not None and 20 * cr < 19 * prev[0] and prev[0] - cr >= DROP_MIN:
            gap = request_time[d["request_event_id"]] - prev[1]
            cause = seen[0] if seen else ("ttl_expired" if gap > ttl else "unknown")
            out.append({"request_event_id": d["request_event_id"], "likely_cause": cause})
        prev, seen = (cr, num(e["time"])), []
    return out


def todos(log: Log) -> list[JsonValue]:
    last = [e for e in log.events if e["type"] == "todos_updated"]
    return arr(obj(last[-1]["data"])["todos"]) if last else []


def children(log: Log) -> list[JsonValue]:
    status: dict[str, str] = {}
    for e in log.events:
        d = obj(e["data"])
        if e["type"] == "agent_spawned":
            status[text(d["child_thread_id"])] = "running"
        elif e["type"] == "agent_finished":
            status[text(d["child_thread_id"])] = text(d["status"])
    return [{"child_thread_id": k, "status": v} for k, v in status.items()]


def team_tasks(log: Log) -> list[JsonValue]:
    tasks: dict[str, Obj] = {}
    for e in log.events:
        d = obj(e["data"])
        t = e["type"]
        if t == "team_task_created":
            tasks[text(d["task_id"])] = {"task_id": d["task_id"], "status": "open"}
        elif t == "team_task_claimed":
            tasks[text(d["task_id"])] = {
                "task_id": d["task_id"],
                "status": "claimed",
                "owner": d["member"],
            }
        elif t == "team_task_updated":
            tid = text(d["task_id"])
            done = d["status"] != "released"
            tasks[tid] = (
                {**tasks[tid], "status": d["status"]}
                if done
                else {"task_id": tid, "status": "open"}
            )
    return list(tasks.values())


def mode(log: Log) -> JsonValue:
    m = obj(_policy(log)["permissions"])["mode"]
    for e in log.events:
        if e["type"] == "mode_changed":
            m = obj(e["data"])["to"]
    return m


def output(log: Log) -> Obj:
    last = [obj(e["data"]) for e in log.events if e["type"] == "output_validated"]
    if not last:
        return {"outcome": "none"}
    d = last[-1]
    return (
        {"outcome": d["outcome"], "value": d["value"]}
        if "value" in d
        else {"outcome": d["outcome"]}
    )

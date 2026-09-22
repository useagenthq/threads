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


INPUT_SIDE = ("input", "cache_read", "cache_write")


def reservation(model: Obj, params: Obj, input_bound: int | None = None) -> int | None:
    """The per-attempt upper bound, or None when no bound is declared."""
    price = obj(model["price"])
    if input_bound is None:
        if model["input_billing_bound"] != "context_window":
            return None
        input_bound = num(model["context_window"])
    top = max(num(price.get(k, 0)) for k in INPUT_SIDE)
    return input_bound * top + num(params["max_tokens"]) * num(price["output"])


def _attempts(log: Log) -> list[tuple[Obj, Obj, Obj | None, Obj | None]]:
    """(request data, epoch settings, response data or None, abandon data or None)."""
    ts = obj(log.events[0]["data"])
    settings: Obj = {"model": ts["model"], "model_params": ts["model_params"]}
    reqs: dict[JsonValue, list[Obj | None]] = {}
    order: list[tuple[JsonValue, Obj, Obj]] = []
    for e in log.events:
        d, t = obj(e["data"]), e["type"]
        if t == "settings_changed":
            settings = obj(d["settings"])
        elif t == "model_request":
            order.append((e["event_id"], d, settings))
            reqs[e["event_id"]] = [None, None]
        elif t in ("model_response", "model_response_recovered"):
            reqs[d["request_event_id"]][0] = d
        elif t == "model_attempt_abandoned":
            reqs[d["request_event_id"]][1] = d
    return [(d, st, reqs[k][0], reqs[k][1]) for k, d, st in order]


def _not_billed(abandon: Obj | None) -> bool:
    if abandon is None:
        return False
    return abandon["provider_outcome"] == "not_sent" or abandon.get("billing") == "not_billed"


def _response_cost(usage: Obj, price: Obj, bound: int | None) -> tuple[int, int | None]:
    """(known nanos, upper nanos or None when unbounded) for one response."""
    known = sum(
        num(v) * num(price.get(k, 0)) for f, k in USAGE_FIELDS if (v := usage.get(f)) is not None
    )
    if all(usage.get(f) is not None for f, _ in USAGE_FIELDS if f in usage):
        return known, known
    return known, None if bound is None else known + bound


def cost(log: Log) -> Obj:
    """Known cost and a conservative bound. Every potentially sent attempt without a response,
    and every response with unknown usage, is charged at its model-declared reservation."""
    pol = _policy(log)
    models = {(text(obj(m)["provider"]), text(obj(m)["name"])): obj(m) for m in arr(pol["models"])}
    known = upper = 0
    complete = bounded = True
    for req, settings, response, abandon in _attempts(log):
        m = obj(settings["model"])
        model = models[(text(m["provider"]), text(m["name"]))]
        ib = req.get("input_bound_tokens")
        bound = reservation(model, obj(settings["model_params"]), None if ib is None else num(ib))
        if response is None and _not_billed(abandon):
            continue
        k, u = (
            (0, bound)
            if response is None
            else _response_cost(obj(response["usage"]), obj(model["price"]), bound)
        )
        known += k
        complete = complete and u == k
        bounded = bounded and u is not None
        upper += k if u is None else u
    return {
        "currency": pol["currency"],
        "known_nanos": known,
        "upper_bound_nanos": upper,
        "complete": complete,
        "bounded": bounded,
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

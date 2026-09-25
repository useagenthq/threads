# pyright: strict
"""The team op vector (spec/conformance/vectors/team-ops.json, shape in
spec/conformance/team-ops.schema.json): for each store op of the Teams contract, a starting world
(logs and rows), the op and its inputs, the clock, and the expected outcome, appended event types
and row changes. Outcomes and appended types are authored from the rule; the reference executor
(ops_run.py) must reach them, and computes the row changes with the reference index."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .common import CASES, arr, obj, sha, text
from .jcs import JsonValue, canonical
from .ops_run import dump_log, rows, run
from .ops_world import CONSTANTS
from .pieces import dump
from .team_ops_ask import ask_vectors
from .team_ops_dynamic import dynamic_vectors
from .team_ops_host import host_vectors
from .team_ops_life import life_vectors
from .team_ops_mail import consume_vectors
from .team_ops_operator import operator_vectors
from .team_ops_send import cancel_vectors, send_vectors
from .team_ops_start import materialize_vectors, start_vectors
from .team_ops_watch import monitor_vectors, wait_vectors
from .team_ops_woken import woken_vectors

if TYPE_CHECKING:
    from .jcs import Obj
    from .team_ops_worlds import Vec

VECTOR = CASES.parent / "vectors" / "team-ops.json"
DESCRIPTION = (
    "Team store ops (spec/schema/README.md, Teams; design sections 4.5-4.14). Each vector names a "
    "world: every team log (thread and branch ids and its events, prev_hash left out: a runner "
    "seeds them under its own header and chains them) and the index rows those logs rebuild "
    "(team_feed left out). Worlds share most events and rows, so each is stored once, in events "
    "and rows, and a world names it by id (the first 16 hex digits of the sha256 of its RFC "
    "8785 bytes). The runner seeds the world, runs op as the writer of log `by` with "
    "`input` at clock `now` (and the limits in `given`), then compares the op's outcome, the "
    "event types it appended per log, in order (none for a log not listed), and the row changes "
    "per table: rows inserted, rows updated (as they are after), and the keys of rows deleted. "
    "Every appended event also adds its team_feed row. Claims are off. Ops: start, send, "
    "ask, reply, cancel, wait, monitor (a model call: input.call_id and input.args of a "
    "pending tool_call; or an operator request in the team log: request_id, principal, body, "
    "idempotency_key?), "
    "consume, deadline (the worker's step for input.id, an AskId or WaitId), materialize "
    "(input.rebind is what the rebind found), idle and end (the member's own settling "
    "appends). Teams Phase 2 vectors carry a lane (29D, 29E), which runtimes skip until that "
    "build: send, ask, reply and consume on a host team's world (a caller and a host member, "
    "decided by given.rules), turn_failure (a host member's failed turn: input.turn, input.error, "
    "input.hop?), supervise (input.member, input.policy) and delete (a caller's deletion)."
)


def _vectors() -> list[Vec]:
    return [
        *send_vectors(),
        *ask_vectors(),
        *cancel_vectors(),
        *consume_vectors(),
        *start_vectors(),
        *dynamic_vectors(),
        *materialize_vectors(),
        *life_vectors(),
        *woken_vectors(),
        *wait_vectors(),
        *monitor_vectors(),
        *operator_vectors(),
        *host_vectors(),
    ]


def _world(v: Vec) -> Obj:
    logs: Obj = {label: dump_log(log) for label, log in sorted(v.world.logs.items())}
    return {"logs": logs, "rows": rows(v.world)}


def _ref(pool: Obj, value: JsonValue) -> str:
    """A pooled value's id: the first 16 hex digits of the sha256 of its RFC 8785 bytes."""
    ident = sha(canonical(value))[:16]
    pool[ident] = value
    return ident


def _pooled(world: Obj, events: Obj, pool: Obj) -> Obj:
    """Worlds share most events and rows, so each is stored once and named by its id."""
    logs: Obj = {}
    for label, raw in obj(world["logs"]).items():
        log = obj(raw)
        ids: list[JsonValue] = [_ref(events, e) for e in arr(log["events"])]
        logs[label] = {**log, "events": ids}
    tables: Obj = {t: [_ref(pool, r) for r in arr(v)] for t, v in obj(world["rows"]).items()}
    return {"logs": logs, "rows": tables}


def resolve(doc: Obj, name: str) -> Obj:
    """A world with its pooled events and rows in place."""
    world, events, pool = obj(obj(doc["worlds"])[name]), obj(doc["events"]), obj(doc["rows"])
    logs: Obj = {
        label: {**obj(log), "events": [events[text(i)] for i in arr(obj(log)["events"])]}
        for label, log in obj(world["logs"]).items()
    }
    tables: Obj = {t: [pool[text(i)] for i in arr(v)] for t, v in obj(world["rows"]).items()}
    return {"logs": logs, "rows": tables}


def _doc() -> str:
    worlds: Obj = {}
    names: dict[bytes, str] = {}
    events: Obj = {}
    pool: Obj = {}
    vectors: list[JsonValue] = []
    for v in _vectors():
        world = _world(v)
        name = names.setdefault(canonical(world), v.name)
        if name not in worlds:
            worlds[name] = _pooled(world, events, pool)
        entry: Obj = {
            "name": v.name,
            "section": v.section,
            "description": v.description,
            "given": {"world": name, **v.given},
            "now": v.now,
            "op": v.op,
            "by": v.by,
            "input": v.input,
        }
        if v.lane is not None:
            entry["lane"] = v.lane
        got, problems = run(world, entry)
        if problems or got["outcome"] != v.outcome or got["appended"] != v.appended:
            raise AssertionError(
                f"{v.name}: {problems} got {got['outcome']} {got['appended']}, "
                f"authored {v.outcome} {v.appended}"
            )
        vectors.append({**entry, "expect": got})
    doc: Obj = {
        "description": DESCRIPTION,
        "constants": CONSTANTS,
        "vectors": vectors,
        "worlds": worlds,
        "events": dict(sorted(events.items())),
        "rows": dict(sorted(pool.items())),
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_doc(), encoding="utf-8")


def check() -> list[str]:
    """The committed file is what the generator writes, and the executor reaches every vector's
    expectation from the file alone."""
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    problems = [] if _doc() == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]
    if not current:
        return problems
    doc = obj(json.loads(current))
    for raw in arr(doc["vectors"]):
        v = obj(raw)
        got, found = run(resolve(doc, text(obj(v["given"])["world"])), v)
        if got != v["expect"]:
            found.append("the executor's outcome, appended types or row changes differ")
        problems += [f"{VECTOR.name} {text(v['name'])}: {p}" for p in found]
    return problems

# pyright: strict
"""Runs the reference validator (ref_rules.py) over every generated case, corpus and staged: an
accepted case must pass rules 31-45, and a rejected one must not break them before its expected
seq; a case of one of these rules must break it exactly there. Rule 43 is checked across a
`team` case's logs."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from .common import obj, text
from .ref_rules import Check
from .turn_open import mail_renders

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

OURS = re.compile(r"^Rule (3[1-9]|4[0-5])\b")
type Found = tuple[str, int, str] | None  # (log label, seq, why)


def _events(path: pathlib.Path) -> list[Obj] | None:
    out: list[Obj] = []
    for raw in path.read_bytes().split(b"\n"):
        if not raw:
            continue
        try:
            line: JsonValue = json.loads(raw)
        except ValueError:
            return None  # a deliberately broken log: its error comes before any rule
        if isinstance(line, dict) and "seq" in line and "type" in line:
            out.append(line)
    return out


def check_log(label: str, events: list[Obj]) -> Found:
    c = Check()
    for e in events:
        why = c.step(e)
        if why is not None:
            return (label, int(str(e["seq"])), why)
    return None


def _sent(logs: dict[str, list[Obj]]) -> dict[str, Obj]:
    return {
        text(obj(obj(e["data"])["envelope"])["mail_id"]): obj(obj(e["data"])["envelope"])
        for events in logs.values()
        for e in events
        if e["type"] == "message_sent"
    }


def _bounce(events: list[Obj], env: Obj, sent: dict[str, Obj]) -> str | None:
    cause = obj(env["causal"])["event_id"]
    refusal = next((e for e in events if e["event_id"] == cause), None)
    if refusal is None or refusal["type"] != "mail_refused":
        return "43: a bounce's causal is not its mail_refused"
    refused = sent.get(text(obj(refusal["data"])["mail_id"]))
    if refused is None:
        return None
    if env["provenance"] != refused["provenance"]:
        return "43: a bounce's provenance is not its refused mail's"
    if refused["kind"] == "ask":
        ok = env.get("ask_id") == refused["ask_id"] and "result" in env
        return None if ok else "43: an ask's bounce must name the ask and carry the result"
    return "43: only an ask's bounce names an ask" if "ask_id" in env else None


def _key(team: JsonValue, name: JsonValue, generation: JsonValue) -> str:
    return f"{text(team)}/{text(name)}/{generation}"


def _identities(logs: dict[str, list[Obj]]) -> dict[str, set[str]]:
    """Who each log speaks for: `team_log`, or `<team>/<name>/<generation>` (a nested lead has
    two: its member ref in the parent team and its lead ref in its own)."""
    by_thread = {text(es[0]["thread_id"]): label for label, es in logs.items() if es}
    out: dict[str, set[str]] = {label: set() for label in logs}
    for label, es in logs.items():
        for e in es:
            d, t = obj(e["data"]), e["type"]
            if t == "team_opened":
                out[label].add("team_log")
            elif t == "thread_started" and "team" in d:
                out[label].add(_key(obj(d["team"])["id"], d["agent_name"], 1))
            elif t == "member_started" and text(d["thread_id"]) in by_thread:
                m = obj(d["member"])
                out[by_thread[text(d["thread_id"])]].add(
                    _key(m["team"], m["name"], m["generation"])
                )
    return out


def _addressed(e: Obj, own: set[str], sent: dict[str, Obj]) -> str | None:
    """The mail clauses: a receipt copies its sender's envelope and sits in the log `to` names;
    a sent mail sits in the log `from` names; a bounce answers its own mail_refused."""
    d = obj(e["data"])
    env = obj(d["envelope"])
    if e["type"] == "message_received":
        if d["mail_id"] in sent and sent[text(d["mail_id"])] != env:
            return "43: a receipt differs from its sender's mail"
        to = env["to"]
        want = (
            "team_log"
            if to == "team_log"
            else _key(env["team"], obj(to)["name"], obj(to)["generation"])
        )
        return None if want in own else "43: a receipt is not in the log `to` names"
    frm = obj(env["from"])
    want = "team_log" if "operator" in frm else _key(frm["team"], frm["name"], frm["generation"])
    return None if want in own else "43: a mail is not in the log `from` names"


def _team_log_named(opened: Obj, logs: dict[str, list[Obj]]) -> str | None:
    """A team log is the thread and branch its lead's thread_started.team names."""
    team = obj(opened["data"])["team"]
    for es in logs.values():
        for e in es:
            named = obj(e["data"]).get("team") if e["type"] == "thread_started" else None
            if isinstance(named, dict) and named["id"] == team:
                here = (opened["thread_id"], opened["branch_id"])
                ok = here == (named["log_thread_id"], named["log_branch_id"])
                return None if ok else "43: a team log is not the thread its lead names"
    return None


def _cross_one(
    events: list[Obj], logs: dict[str, list[Obj]], sent: dict[str, Obj], own: set[str]
) -> tuple[int, str] | None:
    started = {
        obj(e["data"])["thread_id"]: obj(e["data"])["parent"]
        for es in logs.values()
        for e in es
        if e["type"] == "member_started"
    }
    for e in events:
        d, t = obj(e["data"]), e["type"]
        why = None
        if t == "team_opened":
            why = _team_log_named(e, logs)
        elif t in ("message_received", "message_sent"):
            why = _addressed(e, own, sent)
            if why is None and t == "message_sent" and obj(d["envelope"])["kind"] == "bounce":
                why = _bounce(events, obj(d["envelope"]), sent)
        elif (
            t == "thread_started"
            and e["thread_id"] in started
            and d.get("parent") != started[e["thread_id"]]
        ):
            why = "43: a member's parent is not its member_started's"
        elif t == "user_input" and d.get("mail_id") in sent:
            principal = obj(sent[text(d["mail_id"])]["provenance"])["principal"]
            if obj(e["actor"]).get("principal") != principal:
                why = "43: a task's input principal is not its mail's provenance principal"
        if why is not None:
            return (int(str(e["seq"])), why)
    return None


def _task_turn(events: list[Obj], sent: dict[str, Obj]) -> tuple[int, str] | None:
    """Mail that renders inside a member's task turn belongs to the task's run: its
    root_request is the task envelope's. One log only shows the task turn's principal (rule 34);
    the task envelope, in the starter's log, holds its root request."""
    c = Check()
    task_root: JsonValue = None
    for e in events:
        d, t = obj(e["data"]), e["type"]
        if t == "message_received" and c.turn is not None and c.turn[1] is None:
            env = obj(d["envelope"])
            root = obj(env["provenance"])["root_request"]
            if task_root is not None and mail_renders(env, c.settle) and root != task_root:
                return (int(str(e["seq"])), "43: mail of another run joins a member's task turn")
        if t == "user_input" and d.get("mail_id") in sent:
            task_root = obj(sent[text(d["mail_id"])]["provenance"])["root_request"]
        c.step(e)
    return None


def cross(logs: dict[str, list[Obj]]) -> Found:
    sent = _sent(logs)
    own = _identities(logs)
    for label in sorted(logs):
        found = _cross_one(logs[label], logs, sent, own[label])
        found = found or _task_turn(logs[label], sent)
        if found is not None:
            return (label, found[0], found[1])
    return None


def _case_logs(d: pathlib.Path) -> dict[str, list[Obj]] | None:
    paths = sorted((d / "logs").glob("*.jsonl")) if (d / "logs").is_dir() else [d / "log.jsonl"]
    logs: dict[str, list[Obj]] = {}
    for p in paths:
        if not p.exists():
            return None
        events = _events(p)
        if events is None:
            return None
        logs[p.stem] = events
    return logs


def _judge(name: str, meta: Obj, expected: Obj, logs: dict[str, list[Obj]]) -> str | None:
    found = next((f for f in (check_log(k, v) for k, v in sorted(logs.items())) if f), None)
    found = found or (cross(logs) if len(logs) > 1 else None)
    if expected["outcome"] == "ok":
        return (
            None
            if found is None
            else f"{name}: accepted, but {found[0]}@{found[1]} breaks {found[2]}"
        )
    error = obj(expected["error"])
    if "seq" not in error:
        return None
    seq, label = int(str(error["seq"])), error.get("log", next(iter(logs)))
    if found is not None and (found[0], found[1]) != (label, seq) and found[1] <= seq:
        return (
            f"{name}: expected at {label}@{seq}, but {found[0]}@{found[1]} breaks {found[2]} first"
        )
    if found is None and OURS.match(text(meta["description"])):
        return f"{name}: the reference validator does not catch its rule at {label}@{seq}"
    return None


def ref_check(*roots: pathlib.Path) -> list[str]:
    problems: list[str] = []
    for root in roots:
        for d in sorted(root.iterdir()):
            meta = obj(json.loads((d / "case.json").read_text()))
            expected_path = d / "expected.json"
            if meta["kind"] not in ("reduce", "render", "team") or not expected_path.exists():
                continue
            logs = _case_logs(d)
            if logs is None:
                continue
            why = _judge(d.name, meta, obj(json.loads(expected_path.read_text())), logs)
            if why is not None:
                problems.append(f"reference validator: {why}")
    return problems

# pyright: strict
"""Writing the eval conformance cases (lane 22): each spec/conformance/evals/<name>/ holds a
`cases/` root of saved-case directories, an optional `agents.json` of dry pins for drift, and
`report.json`, the offline report runEvals must return, byte for byte in both languages."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, Unpack

from .common import NOW, sha, text
from .jcs import JsonValue, Obj, canonical
from .log import WRITERS, Log, reduce, set_writer
from .pieces import case, dump

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from .evals_turn import Turn

FRAMEWORK_ONLY = " (framework checks only; pass --agent to detect changes to your agents)"


class CaseOpts(TypedDict, total=False):
    must: list[JsonValue]
    rubric: list[JsonValue]
    simulate: Obj
    simulate_blocked: str
    offline: Obj
    snapshot: Obj
    drop: list[str]
    files: dict[str, JsonValue | bytes]
    old_python: bool
    """As a Python save_case wrote it before lane 22: no sandbox.json, extensions.json or
    line0.json, and an expected file with only the outcome and state."""


def _write(d: pathlib.Path, files: dict[str, JsonValue | bytes]) -> None:
    for name, body in sorted(files.items()):
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(dump(body))


def _meta(name: str, turn: Turn, files: dict[str, JsonValue | bytes], opts: CaseOpts) -> Obj:
    more: Obj = {"model_script": "model.json"}
    if "sandbox.json" in files:
        more["sandbox_script"] = "sandbox.json"
    more["stub_script"] = "stubs.json"
    if "extensions.json" in files:
        more["extension_script"] = "extensions.json"
    more["input"] = {"text": turn.text}
    more["expect"] = {"must": opts.get("must", [{"type": "turn_completed"}]), "expect": []}
    for key in ("rubric", "simulate", "simulate_blocked", "snapshot", "offline"):
        value = opts.get(key)
        if value is not None:
            more[key] = value
    line0 = files.get("line0.json")
    if isinstance(line0, bytes):
        more["line0"] = {"sha256": sha(line0)}
    return {
        **case(name, "log_fork_test", "stub", f"Saved case {name} (lane 22 eval fixture)."),
        **more,
    }


def saved_case(
    root: pathlib.Path,
    name: str,
    scenario: Callable[[Log], Turn],
    **opts: Unpack[CaseOpts],
) -> None:
    """<root>/<name>/ exactly as saveCase writes it: one log and expected file per writer."""
    d = root / name
    files: dict[str, JsonValue | bytes] = {}
    turn: Turn | None = None
    for impl in WRITERS:
        set_writer(impl)
        try:
            turn = scenario(Log())
        finally:
            set_writer(WRITERS[0])
        files[f"log.{impl}.jsonl"] = turn.prefix.export()
        expected: Obj = {"outcome": "ok", "state": reduce(turn.prefix, NOW)}
        if not opts.get("old_python", False):
            expected["appended"] = turn.appended()
            expected["stubs"] = {"consumed": len(turn.stubs), "unmatched": 0}
        files[f"expected.{impl}.json"] = expected
    if turn is None:
        raise AssertionError("a saved case has a turn")
    files |= turn.files()
    if opts.get("old_python", False):
        for newer in ("sandbox.json", "extensions.json", "line0.json"):
            files.pop(newer, None)
    for h, b in turn.log.artifacts.items():
        files[f"artifacts/{h}"] = b
    files["case.json"] = _meta(name, turn, files, opts)
    files |= opts.get("files", {})
    for dropped in opts.get("drop", []):
        files.pop(dropped, None)
    d.mkdir(parents=True)
    _write(d, files)


# ---------- the expected report ----------
RERUN_OK: Obj = {
    "ok": True,
    "unmatched": [],
    "script_left": 0,
    "stubs_unmatched": 0,
    "unrecorded_calls": 0,
    "unrecorded_hooks": 0,
}


def passed(name: str, **checks: JsonValue) -> Obj:
    return {
        "name": name,
        "status": "passed",
        "checks": {"replay": {"ok": True}, "rerun": RERUN_OK, **checks},
    }


def result(name: str, status: str, reason: str | None, checks: Obj) -> Obj:
    out: Obj = {"name": name, "status": status}
    if reason is not None:
        out["reason"] = reason
    out["checks"] = checks
    return out


def stale(name: str, reason: str, drift: Obj) -> Obj:
    checks: Obj = {"replay": {"ok": True}, "rerun": RERUN_OK, "drift": drift}
    return result(name, "stale", reason, checks)


def skipped(name: str, reason: str) -> Obj:
    return result(name, "skipped", reason, {"replay": {"ok": True}})


def _summary(counts: dict[str, int], agents: bool) -> str:
    parts = [f"{counts['passed']} passed", f"{counts['failed']} failed"]
    for key, label in (("stale", "stale"), ("skipped", "skipped")):
        if counts[key] > 0:
            parts.append(f"{counts[key]} {label}")
    if counts["errors"] > 0:
        parts.append(f"{counts['errors']} error{'' if counts['errors'] == 1 else 's'}")
    line = ", ".join(parts)
    return line if agents else line + FRAMEWORK_ONLY


def report(cases: list[Obj], *, agents: bool = False) -> bytes:
    """The offline report: counts, summary and ok, as canonical JSON plus a newline."""
    statuses = [text(c["status"]) for c in cases]
    counts = {
        "passed": statuses.count("passed"),
        "failed": statuses.count("failed"),
        "stale": statuses.count("stale"),
        "skipped": statuses.count("skipped"),
        "errors": statuses.count("error"),
        "not_run": statuses.count("not_run"),
    }
    body: Obj = {
        "format": "threads-eval",
        "format_version": 1,
        "summary": _summary(counts, agents),
        "ok": counts["failed"] + counts["errors"] + counts["not_run"] == 0,
        **counts,
        "model_calls": {"agent": 0, "user": 0, "judge": 0},
        "cost": None,
        "cases": list[JsonValue](cases),
    }
    return canonical(body) + b"\n"


def write_eval(
    root: pathlib.Path,
    name: str,
    build: Callable[[pathlib.Path], list[Obj]],
    agents: list[JsonValue] | None = None,
) -> None:
    """spec/conformance/evals/<name>/: its cases, its dry pins, and the report they give."""
    d = root / name
    (d / "cases").mkdir(parents=True)
    cases = build(d / "cases")
    if agents is not None:
        (d / "agents.json").write_text(dump(agents))
    (d / "report.json").write_bytes(report(cases, agents=agents is not None))

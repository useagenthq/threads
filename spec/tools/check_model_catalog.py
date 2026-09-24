#!/usr/bin/env python3
# pyright: strict
"""Keep each model catalog (spec/models/<provider>.v1.json) append-only against its lock.

    python3 spec/tools/check_model_catalog.py             # fail on any change to a released entry
    python3 spec/tools/check_model_catalog.py --add       # also lock new entries and withdrawals
    python3 spec/tools/check_model_catalog.py --refresh   # also move a locked `verified` forward

The lock (<provider>.v1.lock.json) holds, per id, the sha256 of the RFC 8785 form of the entry's
behavioral fields (id, limits, alias_of) and its last accepted `verified` date, and per withdrawal
the sha256 of the whole withdrawal. A released entry's behavior never changes; its evidence
(`source`, `verified`) may, but a date only moves forward and only with --refresh, so the PR that
moves it carries the refreshed lock line. Schema checks are the runtime parsers' job (both
languages parse every embedded catalog at load). Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fixtures.jcs import canonical

if TYPE_CHECKING:
    from fixtures.jcs import JsonValue

MODELS = pathlib.Path(__file__).resolve().parents[2] / "spec" / "models"
BEHAVIOR = ("id", "max_input_tokens", "max_output_tokens", "alias_of")

type Obj = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Outcome:
    lock: Obj
    """The lock after --add / --refresh (unchanged without them)."""
    problems: tuple[str, ...]


def _obj(v: JsonValue) -> Obj:
    return v if isinstance(v, dict) else {}


def _objs(v: JsonValue) -> list[Obj]:
    return [_obj(x) for x in v] if isinstance(v, list) else []


def digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def behavior(entry: Obj) -> str:
    return digest({k: v for k, v in entry.items() if k in BEHAVIOR})


def _entry(entry: Obj, locked: JsonValue, refresh: bool) -> tuple[JsonValue, list[str]]:
    """One entry against its lock line: the line to keep, and problems."""
    name, line = entry.get("id"), _obj(locked)
    if line.get("behavior_sha256") != behavior(entry):
        return locked, [f"{name}: a released entry's id, limits and alias_of never change"]
    was, now = str(line.get("verified")), str(entry.get("verified"))
    if now == was:
        return locked, []
    if now < was:
        return locked, [f"{name}: verified {now} is earlier than the locked {was}"]
    if not refresh:
        return locked, [f"{name}: verified moved to {now}; run check_model_catalog.py --refresh"]
    return {**line, "verified": now}, []


def check(catalog: Obj, lock: Obj, *, add: bool = False, refresh: bool = False) -> Outcome:
    """The catalog against its lock; with add/refresh, the lock those flags produce."""
    locked, entries = _obj(lock.get("entries")), _obj(lock.get("entries")).copy()
    problems: list[str] = []
    present = {str(e.get("id")): e for e in _objs(catalog.get("entries"))}
    problems += [f"{name}: a locked entry was removed" for name in locked if name not in present]
    for name, entry in present.items():
        if name in locked:
            entries[name], found = _entry(entry, locked[name], refresh)
            problems += found
        elif add:
            entries[name] = {"behavior_sha256": behavior(entry), "verified": entry.get("verified")}
        else:
            problems.append(f"{name}: not locked yet; run check_model_catalog.py --add")
    sealed, withdrawn = _obj(lock.get("withdrawn")), _obj(lock.get("withdrawn")).copy()
    listed = {str(w.get("id")): digest(w) for w in _objs(catalog.get("withdrawn"))}
    for name, sha in sealed.items():
        if name not in listed:
            problems.append(f"{name}: a locked withdrawal was removed")
        elif listed[name] != sha:
            problems.append(f"{name}: a locked withdrawal's reason and use never change")
    for name, sha in listed.items():
        if name in sealed:
            continue
        if add:
            withdrawn[name] = sha
        else:
            problems.append(f"{name}: withdrawal not locked yet; run check_model_catalog.py --add")
    return Outcome({"entries": entries, "withdrawn": withdrawn}, tuple(problems))


def _read(path: pathlib.Path) -> Obj:
    return _obj(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}


def main(argv: list[str]) -> int:
    flags = set(argv)
    if not flags <= {"--add", "--refresh"}:
        print(__doc__)
        return 2
    failed = 0
    for path in sorted(MODELS.glob("*.v1.json")):
        lock_path = path.with_name(path.name.replace(".v1.json", ".v1.lock.json"))
        lock = _read(lock_path)
        out = check(_read(path), lock, add="--add" in flags, refresh="--refresh" in flags)
        for problem in out.problems:
            print(f"{path.name}: {problem}", file=sys.stderr)
            failed = 1
        if not out.problems and out.lock != lock:
            lock_path.write_text(json.dumps(out.lock, indent=2, sort_keys=True) + "\n")
    return failed


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

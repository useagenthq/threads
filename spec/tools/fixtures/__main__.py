# pyright: strict
"""Entry point for `python -m fixtures`; spec/tools/gen_fixtures.py runs it."""

from __future__ import annotations

import filecmp
import json
import pathlib
import shutil
import sys
import tempfile

from . import (
    agents,
    changes,
    content,
    context,
    coverage,
    effects,
    extras,
    forks,
    host,
    integrity,
    ladder,
    models,
    policy,
    recovery,
    renders,
    rules,
    structure,
)
from .common import CASES, sha
from .integrity import FOREIGN_WRITER
from .jcs import selftest
from .log import WRITERS, set_writer

FAMILIES = (
    integrity,
    effects,
    recovery,
    forks,
    rules,
    renders,
    host,
    changes,
    content,
    context,
    models,
    agents,
    policy,
    ladder,
    extras,
    structure,
)


def _diff(generated: pathlib.Path, committed: pathlib.Path) -> list[str]:
    diffs: list[str] = []

    def walk(c: filecmp.dircmp[str], rel: str) -> None:
        diffs.extend(f"{rel}{n}: only in generated" for n in c.left_only)
        diffs.extend(f"{rel}{n}: only in repo" for n in c.right_only)
        for n in c.common_files:
            left, right = pathlib.Path(c.left) / n, pathlib.Path(c.right) / n
            if left.read_bytes() != right.read_bytes():
                diffs.append(f"{rel}{n}: differs")
        for n, sub in c.subdirs.items():
            walk(sub, f"{rel}{n}/")

    walk(filecmp.dircmp(generated, committed), "")
    return diffs


def _build(out: pathlib.Path) -> None:
    out.mkdir()
    for family in FAMILIES:
        family.build(out)


def _split_recover(out: pathlib.Path, other: pathlib.Path) -> None:
    """Recover cases append, and only the header's writer may: each keeps one
    log and expected file per implementation. Everything else in the case must be identical."""
    py, ts = WRITERS
    for d in out.iterdir():
        if json.loads((d / "case.json").read_text())["kind"] != "recover":
            continue
        o = other / d.name
        logs = {py: d / "log.jsonl", ts: o / "log.jsonl"}
        if d.name == FOREIGN_WRITER:  # each runner gets the other implementation's log
            logs = {py: logs[ts], ts: logs[py]}
        for impl, log in logs.items():
            (d / f"log.{impl}.jsonl").write_bytes(log.read_bytes())
        for impl, src in ((py, d), (ts, o)):
            (d / f"expected.{impl}.json").write_bytes((src / "expected.json").read_bytes())
        for name in ("log.jsonl", "expected.json"):
            (d / name).unlink()
            (o / name).unlink()
        if sorted(_diff(d, o)) != [f"{n}: only in generated" for n in _per_impl_files(d)]:
            raise AssertionError(f"{d.name}: differs between writers beyond log and expected")


def _per_impl_files(d: pathlib.Path) -> list[str]:
    return sorted(f.name for f in d.iterdir() if f.name.startswith(("log.threads-", "expected.")))


def _generate(out: pathlib.Path) -> None:
    selftest()
    _build(out)
    with tempfile.TemporaryDirectory() as tmp:
        other = pathlib.Path(tmp) / "cases"
        set_writer(WRITERS[1])
        try:
            _build(other)
        finally:
            set_writer(WRITERS[0])
        _split_recover(out, other)
    for f in out.rglob("*.json"):
        json.loads(f.read_text())
    for f in out.rglob("artifacts/*"):
        if sha(f.read_bytes()) != f.name:
            raise AssertionError(f"artifact name is not its hash: {f}")


def main() -> int:
    if sys.argv[1:] not in ([], ["--check"]):
        print(__doc__)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "cases"
        _generate(out)
        problems = coverage.check(out)
        for p in problems:
            print(f"coverage.json: {p}")
        if problems:
            return 1
        if sys.argv[1:] == ["--check"]:
            diffs = _diff(out, CASES)
            for d in diffs:
                print(d)
            print("fixtures up to date" if not diffs else f"{len(diffs)} difference(s)")
            return 1 if diffs else 0
        shutil.rmtree(CASES, ignore_errors=True)
        shutil.copytree(out, CASES)
        print(f"wrote {sum(1 for _ in CASES.iterdir())} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())

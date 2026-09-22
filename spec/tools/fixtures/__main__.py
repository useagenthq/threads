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
)
from .common import CASES, sha
from .jcs import selftest

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


def _generate(out: pathlib.Path) -> None:
    selftest()
    out.mkdir()
    for family in FAMILIES:
        family.build(out)
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

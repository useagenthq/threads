#!/usr/bin/env python3
# pyright: strict
"""The API surface gate: the packages match spec/api.json, gaps only shrink, and every function,
required method and required option has a named test that passed.

    check_surface.py --lang ts|py --junit FILE --bootstrap BASE_SHA [--release]
    check_surface.py --lang ts|py --junit FILE --baseline GAPS --baseline-api API [--release]

Stdlib only. tsc proves TypeScript existence over the generated surface.ts; for --lang ts this
checks the registries only. For --lang py it imports `threads`, so run it in the project
environment (`uv run --project python`). --bootstrap is only for the one PR that adds
spec/api-surface-gaps.json (the base commit doesn't have it); after that, --baseline takes the
base commit's gaps file and api.json, and a new gap passes only for a member new in this PR.

The gate checks existence, callability, export entries and required flags, not signatures:
parameter and return types are left to each language's type checker and tests.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING

from factory_coverage import factory_evidence
from surface_changed import check_changed
from surface_contract import Gap, Member, members, obj, parse_gaps
from surface_coverage import Owed, check_coverage

if TYPE_CHECKING:
    from check_api import Json

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "spec" / "api.json"
GAPS = ROOT / "spec" / "api-surface-gaps.json"
COVERAGE = ROOT / "spec" / "api-coverage.json"
DECISIONS = ROOT / "spec" / "api-surface-factory-decisions.json"
GAPS_IN_GIT = "spec/api-surface-gaps.json"


def _read(path: pathlib.Path) -> tuple[Json, list[str]]:
    try:
        doc: Json = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return None, [f"{path}: {e}"]
    return doc, []


def _label(g: Gap) -> str:
    return f"{g.name} ({g.lang}, {_kind(g.kind, g.at)}, lane {g.lane})"


def _kind(kind: str, at: str | None) -> str:
    return f"{kind} (exported from {at})" if at else kind


def check_bootstrap(base: str) -> list[str]:
    """--bootstrap seeds the registry once: refused when the base commit already has it."""
    found = subprocess.run(  # noqa: S603 - a fixed argv; base is a commit name, not shell text
        ["git", "cat-file", "-e", f"{base}:{GAPS_IN_GIT}"],  # noqa: S607 - git from PATH
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if found.returncode == 0:
        return [f"surface gate: --bootstrap refused: {base} already has {GAPS_IN_GIT}"]
    return []


def check_baseline(
    gaps: list[Gap], base_gaps: list[Gap], base_contract: dict[str, Member], lang: str
) -> list[str]:
    """A gap added since the base is allowed only for a member the base contract lacks, or for a
    member of an owner the base listed missing whole: the owner landed without all of it, so the
    gap narrows to what is still missing. A narrowed gap keeps its owner's lane, or the lane the
    base already gives that member in another language."""
    before = {g.key for g in base_gaps}
    whole = {g.name: g.lane for g in base_gaps if g.lang == lang and g.kind == "missing"}
    others = {g.name: g.lane for g in base_gaps if g.lang != lang}

    def lanes(name: str) -> set[str]:
        parts = name.split(".")
        owners = (".".join(parts[:i]) for i in range(1, len(parts)))
        found = {whole[o] for o in owners if o in whole}
        return found | ({others[name]} if found and name in others else set())

    errs: list[str] = []
    for g in gaps:
        if g.kind == "changed" or g.lang != lang or g.key in before:
            continue  # a changed gap is surface_changed.py's
        if g.name not in base_contract:
            continue
        allowed = lanes(g.name)
        if not allowed:
            errs.append(
                f"surface gate: new gap {_label(g)} for a member that exists at the base; "
                "restore the member instead of listing it"
            )
        elif g.lane not in allowed:
            errs.append(
                f"surface gate: narrowed gap {_label(g)} takes lane {g.lane}; "
                f"its owner's is {', '.join(sorted(allowed))}"
            )
    return errs


def check_python(
    contract: dict[str, Member], gaps: list[Gap], entries: dict[str, str]
) -> list[str]:
    from surface_py import Package, PythonSurface  # noqa: PLC0415 - imports threads

    found = PythonSurface(contract, Package(entries)).findings()
    listed = {(g.name, g.kind, g.at): g for g in gaps if g.lang == "py" and g.kind != "changed"}
    errs = [
        f"surface gate: {name} (py) is {_kind(kind, at)} and not listed in {GAPS_IN_GIT}; "
        "fix the package, or list the gap with its owning lane"
        for name, kind, at in sorted(found - listed.keys(), key=str)
    ]
    errs += [
        f"surface gate: {_label(listed[k])} is listed but fixed; delete its entry"
        for k in sorted(listed.keys() - found, key=str)
    ]
    return errs


def _mode(args: argparse.Namespace) -> list[str]:
    base: str | None = args.bootstrap
    baseline: pathlib.Path | None = args.baseline
    baseline_api: pathlib.Path | None = args.baseline_api
    if (base is None) == (baseline is None):
        return ["surface gate: pass exactly one of --bootstrap BASE_SHA or --baseline GAPS"]
    if (baseline is None) != (baseline_api is None):
        return ["surface gate: --baseline needs --baseline-api (the base commit's api.json)"]
    return check_bootstrap(base) if base is not None else []


def _baseline(args: argparse.Namespace, gaps: list[Gap], lang: str) -> list[str]:
    baseline: pathlib.Path | None = args.baseline
    baseline_api: pathlib.Path | None = args.baseline_api
    if baseline is None or baseline_api is None:
        return []
    base_api, errs = _read(baseline_api)
    base_doc, more = _read(baseline)
    if errs or more:
        return errs + more
    base_contract = members(base_api)
    base_gaps, errs = parse_gaps(base_doc, base_contract, f"{baseline}")
    return errs or check_baseline(gaps, base_gaps, base_contract, lang)


def _base(args: argparse.Namespace) -> tuple[Json, list[Gap]] | None:
    """The base commit's api.json and gaps, when the gate runs against a baseline."""
    baseline: pathlib.Path | None = args.baseline
    baseline_api: pathlib.Path | None = args.baseline_api
    if baseline is None or baseline_api is None:
        return None
    base_api, errs = _read(baseline_api)
    base_doc, more = _read(baseline)
    if errs or more:
        return None
    return base_api, parse_gaps(base_doc, members(base_api), f"{baseline}")[0]


def run(args: argparse.Namespace) -> list[str]:
    lang: str = args.lang
    errs = _mode(args)
    if errs:
        return errs
    api, errs = _read(API)
    gaps_doc, more = _read(GAPS)
    if errs or more:
        return errs + more
    contract = members(api)
    gaps, errs = parse_gaps(gaps_doc, contract, GAPS_IN_GIT)
    if errs:
        return errs
    errs += _baseline(args, gaps, lang)
    errs += check_changed(gaps, api, _base(args))
    if lang == "py":
        # A package absent from one language omits that key (schema/README, "The API surface
        # gate"), and its members carry lang; there is no module to import for it.
        entries = {
            k: str(py)
            for k, v in obj(obj(api).get("packages")).items()
            if (py := obj(v).get("py")) is not None
        }
        errs += check_python(contract, gaps, entries)
    coverage, more = _read(COVERAGE)
    decisions, also = _read(DECISIONS)
    owed = factory_evidence(api, decisions)
    errs += more + also or check_coverage(coverage, Owed(contract, gaps, owed), lang, args.junit)
    if args.release and gaps:
        errs += [f"surface gate: release with an open gap: {_label(g)}" for g in gaps]
    return errs


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=("ts", "py"), required=True)
    parser.add_argument("--junit", type=pathlib.Path, required=True)
    parser.add_argument("--bootstrap", metavar="BASE_SHA")
    parser.add_argument("--baseline", type=pathlib.Path)
    parser.add_argument("--baseline-api", type=pathlib.Path)
    parser.add_argument("--release", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    errs = run(parse(argv))
    for e in errs:
        print(e)
    print("api surface ok" if not errs else f"{len(errs)} problem(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

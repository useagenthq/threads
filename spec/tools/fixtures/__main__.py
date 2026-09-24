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
    catalog_tools,
    changes,
    channels,
    children,
    content,
    context,
    cost_usage,
    coverage,
    effects,
    extras,
    fallbacks,
    forks,
    guards,
    handoff_transcripts,
    host,
    integrity,
    ladder,
    legacy_run,
    legacy_wake_rows,
    memory,
    models,
    open_turn,
    output_schemas,
    outputs,
    policy,
    recovery,
    ref_team,
    renders,
    rules,
    run_cases,
    skills,
    structure,
    styles,
    summaries,
    team_bindings,
    team_cancel_rule,
    team_edges,
    team_nested,
    team_operator,
    team_ops,
    team_rebind,
    team_replay,
    team_rules,
    team_wire,
    teams,
    thread_methods,
    tool_groups,
    tool_inputs,
    wakes,
)
from .common import CASES, STAGED, sha
from .integrity import FOREIGN_WRITER
from .jcs import selftest
from .log import WRITERS, set_writer

FAMILIES = (
    integrity,
    effects,
    recovery,
    open_turn,
    forks,
    rules,
    renders,
    host,
    channels,
    changes,
    content,
    context,
    models,
    fallbacks,
    outputs,
    output_schemas,
    agents,
    teams,
    children,
    policy,
    ladder,
    extras,
    cost_usage,
    structure,
    guards,
    summaries,
    memory,
    catalog_tools,
    skills,
    thread_methods,
    styles,
    wakes,
    team_rules,
    team_bindings,
    team_edges,
    team_replay,
    team_rebind,
    team_operator,
    team_nested,
    legacy_run,
    legacy_wake_rows,
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


# Staged families, by the phase whose build moves them into FAMILIES: the Teams Phase 1 read side
# (lanes 21A and 21B). Phase 0 (the legacy wake) moved legacy_run and legacy_wake_rows.
STAGED_PHASE_1 = (
    team_bindings.build_staged,
    team_cancel_rule.build,
    run_cases.build,
)


def _build_staged(out: pathlib.Path) -> None:
    """Cases for an approved spec whose build hasn't landed: generated and checked like the
    corpus, but no runner reads them until the build moves each family into FAMILIES."""
    out.mkdir()
    for build in STAGED_PHASE_1:
        build(out)


APPENDING_KINDS = frozenset({"recover", "stub"})


def _split_recover(out: pathlib.Path, other: pathlib.Path) -> None:
    """Recover and stub cases append, and only the header's writer may: each
    keeps one log and expected file per implementation. Everything else must be identical."""
    py, ts = WRITERS
    for d in out.iterdir():
        if json.loads((d / "case.json").read_text())["kind"] not in APPENDING_KINDS:
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
        staged = pathlib.Path(tmp) / "staged"
        _build_staged(staged)
        problems = coverage.check(out) + ref_team.ref_check(out, staged)
        if sys.argv[1:] == ["--check"]:
            problems += (
                tool_inputs.check() + tool_groups.check() + team_wire.check() + team_ops.check()
            )
            problems += handoff_transcripts.check()
        for p in problems:
            print(f"coverage.json: {p}")
        if problems:
            return 1
        if sys.argv[1:] == ["--check"]:
            diffs = _diff(out, CASES) + [f"staged/{d}" for d in _diff(staged, STAGED)]
            for d in diffs:
                print(d)
            print("fixtures up to date" if not diffs else f"{len(diffs)} difference(s)")
            return 1 if diffs else 0
        tool_inputs.write()
        tool_groups.write()
        team_wire.write()
        team_ops.write()
        handoff_transcripts.write()
        for built, dest in ((out, CASES), (staged, STAGED)):
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(built, dest)
        print(f"wrote {sum(1 for _ in CASES.iterdir())} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())

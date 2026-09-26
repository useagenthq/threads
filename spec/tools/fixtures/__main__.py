# pyright: strict
"""Entry point for `python -m fixtures`; spec/tools/gen_fixtures.py runs it."""

from __future__ import annotations

import filecmp
import json
import pathlib
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING

from . import (
    agent_pins,
    agents,
    anthropic_requests,
    call_order,
    catalog_tools,
    changes,
    channels,
    children,
    content,
    context,
    cost_usage,
    coverage,
    dynamic,
    e2b_wire,
    effects,
    eval_simulate_vectors,
    eval_vectors,
    evals,
    evals_drift,
    evals_formats,
    evals_hooks,
    evals_simulate,
    extras,
    fallbacks,
    forks,
    guards,
    handoff_transcripts,
    host,
    host_cases,
    host_ends,
    host_rules,
    host_sends,
    integrity,
    ladder,
    legacy_run,
    legacy_wake_rows,
    memory,
    models,
    open_turn,
    otel,
    output_schemas,
    outputs,
    policy,
    policy_shell,
    pos_int_vector,
    prompt_cache,
    questions,
    recovery,
    ref_team,
    renders,
    rules,
    run_cases,
    skills,
    structure,
    styles,
    summaries,
    tar_vectors,
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
    tool_calls_gone,
    tool_groups,
    tool_inputs,
    tool_search,
    tool_search_loop,
    tool_search_rules,
    tool_search_vectors,
    tool_sets,
    ui_cases,
    ui_cases_live,
    ui_vectors,
    wake_bars,
    wakes,
    workspace_exclude,
)
from .common import CASES, STAGED, STAGED_PHASE_2_DIR, sha
from .integrity import FOREIGN_WRITER
from .jcs import selftest
from .log import WRITERS, set_writer

if TYPE_CHECKING:
    from collections.abc import Callable

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
    tool_sets,
    tool_calls_gone,
    tool_search,
    tool_search_rules,
    tool_search_loop,
    content,
    context,
    models,
    prompt_cache,
    fallbacks,
    outputs,
    output_schemas,
    agents,
    teams,
    children,
    policy,
    policy_shell,
    call_order,
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
    wake_bars,
    wakes,
    team_rules,
    team_bindings,
    team_edges,
    team_replay,
    team_rebind,
    team_operator,
    team_nested,
    team_cancel_rule,
    legacy_run,
    legacy_wake_rows,
    run_cases,
    dynamic,
    questions,
    host_sends,
    ui_cases,
    ui_cases_live,
)


def _diff(generated: pathlib.Path, committed: pathlib.Path) -> list[str]:
    # git keeps no empty directory: a committed directory with nothing in it is absent.
    if not committed.exists():
        return [f"{n}: only in generated" for n in sorted(p.name for p in generated.iterdir())]
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


# Staged families, by the phase whose build moves them into FAMILIES. Phase 0 (the legacy wake)
# moved legacy_run and legacy_wake_rows; lane 21D (team run completion) moved run_cases; lane 21E
# (cancel application) moved team_cancel_rule. None is staged now.
STAGED_PHASE_1: tuple[Callable[[pathlib.Path], None], ...] = ()
# Teams Phase 2 (lane 29): its own directory, since both runtimes refuse its forms until the build
# (unsupported_critical_event) and so can't read these logs as they read staged/.
STAGED_PHASE_2 = (host_cases.build, host_rules.build, host_ends.build)


def _build_staged(out: pathlib.Path) -> None:
    """Cases for an approved spec whose build hasn't landed: generated and checked like the
    corpus, but no runner reads them until the build moves each family into FAMILIES."""
    out.mkdir()
    for build in STAGED_PHASE_1:
        build(out)


def _build_staged_phase_2(out: pathlib.Path) -> None:
    out.mkdir()
    for build in STAGED_PHASE_2:
        build(out)


EVALS = CASES.parent / "evals"


def _build_evals(out: pathlib.Path) -> None:
    """spec/conformance/evals: saved cases and the offline report runEvals gives for them."""
    out.mkdir()
    for build in (
        evals.build,
        evals_drift.build,
        evals_hooks.build,
        evals_formats.build,
        evals_simulate.build,
    ):
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


def _write_all(
    out: pathlib.Path,
    staged: pathlib.Path,
    phase2: pathlib.Path,
    traces: pathlib.Path,
    built_evals: pathlib.Path,
) -> None:
    """Write every vector file, then replace each committed fixture tree with what was built."""
    tool_inputs.write()
    tool_groups.write()
    team_wire.write()
    team_ops.write()
    handoff_transcripts.write()
    agent_pins.write()
    anthropic_requests.write()
    dynamic.write()
    tool_search_vectors.write()
    e2b_wire.write()
    tar_vectors.write()
    workspace_exclude.write()
    questions.write()
    ui_vectors.write()
    pos_int_vector.write()
    eval_vectors.write()
    eval_simulate_vectors.write()
    otel_parts = [(traces / part, otel.OTEL / part) for part in otel.PARTS]
    staged_all = ((staged, STAGED), (phase2, STAGED_PHASE_2_DIR))
    for built, dest in ((out, CASES), *staged_all, (built_evals, EVALS), *otel_parts):
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(built, dest)
    print(f"wrote {sum(1 for _ in CASES.iterdir())} cases")


def main() -> int:
    if sys.argv[1:] not in ([], ["--check"]):
        print(__doc__)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "cases"
        _generate(out)
        staged = pathlib.Path(tmp) / "staged"
        _build_staged(staged)
        phase2 = pathlib.Path(tmp) / "staged-phase-2"
        _build_staged_phase_2(phase2)
        traces = pathlib.Path(tmp) / "otel"
        otel.build(traces)
        built_evals = pathlib.Path(tmp) / "evals"
        _build_evals(built_evals)
        problems = coverage.check(out) + ref_team.ref_check(out, staged, phase2)
        if sys.argv[1:] == ["--check"]:
            problems += (
                tool_inputs.check() + tool_groups.check() + team_wire.check() + team_ops.check()
            )
            problems += handoff_transcripts.check() + questions.check()
            problems += agent_pins.check()
            problems += anthropic_requests.check() + dynamic.check()
            problems += tool_search_vectors.check() + e2b_wire.check()
            problems += ui_vectors.check() + tar_vectors.check() + pos_int_vector.check()
            problems += workspace_exclude.check()
        for p in problems:
            print(f"coverage.json: {p}")
        if problems:
            return 1
        if sys.argv[1:] == ["--check"]:
            diffs = _diff(out, CASES) + [f"staged/{d}" for d in _diff(staged, STAGED)]
            diffs += [f"staged-phase-2/{d}" for d in _diff(phase2, STAGED_PHASE_2_DIR)]
            for part in otel.PARTS:
                diffs += [f"otel/{part}/{d}" for d in _diff(traces / part, otel.OTEL / part)]
            diffs += [f"evals/{d}" for d in _diff(built_evals, EVALS)] + eval_vectors.check()
            diffs += eval_simulate_vectors.check()
            for d in diffs:
                print(d)
            print("fixtures up to date" if not diffs else f"{len(diffs)} difference(s)")
            return 1 if diffs else 0
        _write_all(out, staged, phase2, traces, built_evals)
    return 0


if __name__ == "__main__":
    sys.exit(main())

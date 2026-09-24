# pyright: strict
"""spec/otel/: the OpenTelemetry goldens (cases/) and vectors (vectors/)."""

from __future__ import annotations

import pathlib

from . import otel_parks, otel_trees, otel_turns, otel_vectors

OTEL = pathlib.Path(__file__).resolve().parents[2] / "otel"
PARTS = ("cases", "vectors")


def build(out: pathlib.Path) -> None:
    """Writes out/cases and out/vectors, the generated parts of spec/otel/."""
    cases = out / "cases"
    cases.mkdir(parents=True)
    for family in (otel_turns, otel_parks, otel_trees):
        family.build(cases)
    otel_vectors.build(out / "vectors")

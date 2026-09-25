# pyright: strict
"""The `changed` gaps of the API surface gate (spec/schema/README.md, "The gaps registry"): a
member that exists in both languages but whose contract (a signature, a returned variant, an
error code, an HTTP route) changed ahead of its build. The gate can't see signatures, so it checks
the two things it can: a new `changed` gap names a member whose api.json entry differs from the
base commit's, and the docs reference page of every `changed` gap carries its marker, so the
published docs never promise what isn't built. Stdlib only."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from surface_contract import Gap, obj

if TYPE_CHECKING:
    from check_api import Json

DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs" / "content" / "docs" / "reference"


def marker(lane: str) -> str:
    """What the docs generator writes, and the gate looks for, on a changed member's page."""
    return f"**Phase 2, not yet available** (lane {lane})"


def raw_entry(api: Json, name: str) -> Json:
    """The api.json entry a gap names: a type, a type's method or property, or a function."""
    owner, _, member = name.partition(".")
    types, functions = obj(obj(api).get("types")), obj(obj(api).get("functions"))
    if not member:
        return types.get(owner, functions.get(owner))
    t = obj(types.get(owner))
    return obj(t.get("methods")).get(member, obj(t.get("properties")).get(member))


def page(name: str, api: Json) -> pathlib.Path:
    owner = name.partition(".")[0]
    kind = "types" if owner in obj(obj(api).get("types")) else "functions"
    return DOCS / kind / f"{owner}.mdx"


def check_changed(gaps: list[Gap], api: Json, base: tuple[Json, list[Gap]] | None) -> list[str]:
    errs: list[str] = []
    before: set[tuple[str, str, str, str | None]] = (
        set() if base is None else {g.key for g in base[1]}
    )
    for g in (g for g in gaps if g.kind == "changed"):
        doc = page(g.name, api)
        text = doc.read_text(encoding="utf-8") if doc.exists() else ""
        if marker(g.lane) not in text:
            errs.append(
                f"surface gate: {g.name} is changed (lane {g.lane}) but {doc.name} lacks its "
                f"marker {marker(g.lane)!r}; run the docs generator"
            )
        if (
            base is not None
            and g.key not in before
            and raw_entry(api, g.name) == raw_entry(base[0], g.name)
        ):
            errs.append(
                f"surface gate: new changed gap {g.name} ({g.lang}) for a member whose api.json "
                "entry is the base commit's; list only a contract this change makes"
            )
    return errs

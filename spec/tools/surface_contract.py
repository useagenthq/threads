# pyright: strict
"""The API surface contract, read from spec/api.json, and the reviewed gaps registry
(spec/api-surface-gaps.json). Shared by gen_api_surface.py and check_surface.py. Stdlib only.

A member name uses api.json keys: `fn`, `fn.option`, `Type`, `Type.member`, `Type.method.option`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from check_api import Json, callables, camel

if TYPE_CHECKING:
    from collections.abc import Iterator

type Role = Literal["function", "option", "type", "property", "field", "method"]

LANGS = frozenset({"ts", "py"})
KINDS: dict[str, frozenset[Role]] = {
    "missing": frozenset({"function", "option", "type", "property", "field", "method"}),
    "required_mismatch": frozenset({"option", "property", "field", "method"}),
    "placement": frozenset({"type", "method"}),
}
# The owning lane: its file name under plans/specs/lanes/ (NN-name). Every gap has an owner.
LANE = re.compile(r"^\d\d-[a-z0-9-]+$")
GAP_KEYS = frozenset({"name", "lang", "kind", "lane"})
# A placement gap also names the other public entry that exports the member ("at").
PLACEMENT_KEYS = GAP_KEYS | {"at"}


@dataclass(frozen=True, slots=True)
class Member:
    name: str
    role: Role
    langs: frozenset[str]
    required: bool
    package: str
    ts: str
    py: str
    parent: str = ""
    positional: int = 0
    generics: int = 0
    capability: str | None = None


@dataclass(frozen=True, slots=True, order=True)
class Gap:
    name: str
    lang: str
    kind: str
    lane: str
    at: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.name, self.lang, self.kind)


def obj(v: Json) -> dict[str, Json]:
    return v if isinstance(v, dict) else {}


def _list(v: Json) -> list[Json]:
    return v if isinstance(v, list) else []


def _langs(entry: dict[str, Json], within: frozenset[str] = LANGS) -> frozenset[str]:
    lang = entry.get("lang")
    return within & {lang} if isinstance(lang, str) else within


def _options(owner: Member, f: dict[str, Json]) -> Iterator[Member]:
    for p in map(obj, _list(f.get("params"))):
        if p.get("kind") != "option":
            continue
        name = str(p.get("name"))
        yield Member(
            name=f"{owner.name}.{name}",
            role="option",
            langs=_langs(p, owner.langs),
            required=p.get("required") is True,
            package=owner.package,
            ts=camel(name),
            py=name,
            parent=owner.name,
            positional=owner.positional,
        )


def _callable(where: str, f: dict[str, Json], role: Role, package: str) -> Iterator[Member]:
    positional = sum(1 for p in _list(f.get("params")) if obj(p).get("kind") == "positional")
    cap = f.get("capability")
    member = Member(
        name=where,
        role=role,
        langs=_langs(f),
        required=f.get("optional") is not True,
        package=package,
        ts=str(f.get("ts")),
        py=str(f.get("py")),
        parent=where.rpartition(".")[0],
        positional=positional,
        capability=cap if isinstance(cap, str) else None,
    )
    yield member
    yield from _options(member, f)


def _type(name: str, t: dict[str, Json]) -> Iterator[Member]:
    package = str(t.get("package", "core"))
    count = len(_list(t.get("generics")))
    yield Member(name, "type", LANGS, True, package, name, name, generics=count)
    wire = t.get("casing") == "wire"
    for role, key, f in _members_of(t):
        yield Member(
            f"{name}.{key}", role, _langs(f), f.get("required") is True, package,
            key if wire else camel(key), key, parent=name,
        )  # fmt: skip


def _members_of(t: dict[str, Json]) -> Iterator[tuple[Role, str, dict[str, Json]]]:
    """An interface's properties and a data type's fields (a wire type keeps snake_case)."""
    for key, field in obj(t.get("properties")).items():
        yield "property", key, obj(field)
    for key, field in obj(t.get("fields")).items():
        yield "field", key, obj(field)


def members(api: Json) -> dict[str, Member]:
    """Every checked member of the contract, by name."""
    found: list[Member] = []
    types = obj(obj(api).get("types"))
    for name, t in types.items():
        found += _type(name, obj(t))
    for where, _, f in callables(api):
        owner = where.partition(".")[0]
        role: Role = "method" if "." in where else "function"
        package = str(obj(types.get(owner)).get("package", "core")) if role == "method" else None
        found += _callable(where, f, role, package or str(f.get("package", "core")))
    return {m.name: m for m in found}


def needs_coverage(m: Member) -> bool:
    """Functions, required methods and required options carry test evidence."""
    return m.role == "function" or (m.role in ("method", "option") and m.required)


def _applies(gap: Gap, m: Member) -> bool:
    """placement fits a type, or a method's Python capability protocol."""
    if gap.kind == "placement" and m.role == "method":
        return gap.lang == "py" and m.capability is not None
    return m.role in KINDS.get(gap.kind, frozenset())


def _gap(entry: Json, at: str, contract: dict[str, Member]) -> tuple[Gap | None, list[str]]:
    e = obj(entry)
    keys = PLACEMENT_KEYS if e.get("kind") == "placement" else GAP_KEYS
    if frozenset(e) != keys or not all(isinstance(v, str) for v in e.values()):
        return None, [
            f"{at}: a gap is exactly {{name, lang, kind, lane}}, plus at for a placement, "
            "all strings"
        ]
    gap = Gap(
        str(e["name"]),
        str(e["lang"]),
        str(e["kind"]),
        str(e["lane"]),
        str(e["at"]) if "at" in e else None,
    )
    member = contract.get(gap.name)
    errs: list[str] = []
    if member is None:
        errs.append(f"{at}: {gap.name} is not in spec/api.json")
    elif gap.lang not in member.langs:
        errs.append(f"{at}: {gap.name} does not exist in {gap.lang} (lang: {gap.lang!r})")
    elif not _applies(gap, member):
        errs.append(f"{at}: kind {gap.kind!r} does not apply to {member.role} {gap.name}")
    elif gap.at is not None and gap.at not in {m.package for m in contract.values()} - {
        member.package
    }:
        errs.append(f"{at}: at {gap.at!r} is not another package of the contract")
    if not LANE.match(gap.lane):
        errs.append(f"{at}: lane {gap.lane!r} is not a lane name (NN-name)")
    return (None, errs) if errs else (gap, [])


def parse_gaps(doc: Json, contract: dict[str, Member], source: str) -> tuple[list[Gap], list[str]]:
    """The registry's entries, or every problem with it. A duplicate entry is a problem."""
    if not isinstance(doc, list):
        return [], [f"{source}: expected a JSON array of gaps"]
    gaps: list[Gap] = []
    errs: list[str] = []
    for i, entry in enumerate(doc):
        gap, problems = _gap(entry, f"{source}[{i}]", contract)
        errs += problems
        if gap is not None and gap.key in {g.key for g in gaps}:
            errs.append(f"{source}[{i}]: duplicate gap {gap.name} ({gap.lang}, {gap.kind})")
        elif gap is not None:
            gaps.append(gap)
    return gaps, errs

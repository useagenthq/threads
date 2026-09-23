# pyright: strict
"""The factory contract: api_factories.py's rules, and spec/api-surface-factory-decisions.json
against spec/api.json and the adapter sources.

- every function of an adapter package is a contracted factory in the decisions file, and every
  contracted factory is in api.json (a factory not yet installed says `pending`);
- every one-language param of a contracted factory has a decision row for that language
  (factory, option or *, lang), so a
  new language difference can't be installed undecided;
- a review-only default (no observable effect) names a defaulted param; every other default
  owes a `<factory>.<option>=default` behavior test in the coverage registry (factory_coverage.py);
- every decision names a known factory;
- the refusal source scan (factory_scan.py) over each adapter package with contracted factories.

Stdlib only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from api_factories import check_factories
from factory_scan import scan_problems, source_shape

if TYPE_CHECKING:
    import pathlib

    from api_factories import Json

DECISION_KEYS = frozenset({"factory", "option", "lang", "decision", "owner"})
FACTORY_KEYS = frozenset({"owner", "pending", "review_only"})


def _obj(v: Json | None) -> dict[str, Json]:
    return v if isinstance(v, dict) else {}


def _list(v: Json | None) -> list[Json]:
    return v if isinstance(v, list) else []


def _shape(doc: Json) -> list[str]:
    root = _obj(doc)
    errs = [
        f"decisions: unexpected key {k}"
        for k in root
        if k not in {"$comment", "packages", "factories", "decisions"}
    ]
    for name, p in _obj(root.get("packages")).items():
        errs += source_shape(name, p)
    for name, f in _obj(root.get("factories")).items():
        entry = _obj(f)
        if not isinstance(entry.get("owner"), str) or not set(entry) <= FACTORY_KEYS:
            errs.append(f"decisions factories.{name}: expected {{owner, pending?, review_only?}}")
    for i, d in enumerate(_list(root.get("decisions"))):
        entry = _obj(d)
        if entry.keys() != DECISION_KEYS or entry.get("lang") not in ("ts", "py", "both"):
            errs.append(
                f"decisions decisions[{i}]: expected {sorted(DECISION_KEYS)}, lang ts|py|both"
            )
    return errs


def _installed(api: Json) -> dict[str, dict[str, Json]]:
    """api.json functions of adapter packages, by name."""
    packages = _obj(_obj(api).get("packages"))
    return {
        k: _obj(f)
        for k, f in _obj(_obj(api).get("functions")).items()
        if _obj(packages.get(str(_obj(f).get("package", "core")))).get("kind") == "adapter"
    }


def _params(f: dict[str, Json]) -> list[dict[str, Json]]:
    return [_obj(x) for x in _list(f.get("params"))]


def _contracted(
    name: str, f: dict[str, Json], entry: dict[str, Json], decided: set[tuple[str, str, str]]
) -> list[str]:
    """review_only names defaults; one-language params have a decision."""
    review = _obj(entry.get("review_only"))
    defaults = [str(p.get("name")) for p in _params(f) if "default" in p or "default_doc" in p]
    errs = [
        f"decisions factories.{name}.review_only: {p} has no default in spec/api.json"
        for p in review
        if p not in defaults
    ]
    return errs + [
        f"api.json {name}.{p.get('name')}: a {p.get('lang')}-only param needs a decision row"
        for p in _params(f)
        if "lang" in p
        and not {(name, str(p.get("name")), str(p.get("lang"))), (name, "*", str(p.get("lang")))}
        & decided
    ]


def _factory_problems(api: Json, doc: Json) -> list[str]:
    factories = _obj(_obj(doc).get("factories"))
    decisions = [_obj(d) for d in _list(_obj(doc).get("decisions"))]
    decided = {(str(d.get("factory")), str(d.get("option")), str(d.get("lang"))) for d in decisions}
    installed = _installed(api)
    errs = [
        f"decisions: factory {k} is in spec/api.json; add it to factories"
        for k in installed
        if k not in factories
    ]
    for name, f in factories.items():
        entry = _obj(f)
        if "pending" in entry:
            if name in installed:
                errs.append(f"decisions factories.{name}: installed in spec/api.json; drop pending")
        elif name not in installed:
            errs.append(f"decisions factories.{name}: not in spec/api.json; mark it pending")
        else:
            errs += _contracted(name, installed[name], entry, decided)
    return errs + [
        f"decisions decisions: unknown factory {d.get('factory')}"
        for d in decisions
        if d.get("factory") not in factories
    ]


def _scan(api: Json, doc: Json, root: pathlib.Path) -> list[str]:
    sources = _obj(_obj(doc).get("packages"))
    packages = _obj(_obj(api).get("packages"))
    by_package: dict[str, dict[str, dict[str, Json]]] = {}
    for k, f in _installed(api).items():
        by_package.setdefault(str(f.get("package")), {})[k] = f
    errs: list[str] = []
    for name, factories in sorted(by_package.items()):
        files = _obj(sources.get(name))
        errs += [
            f"decisions packages.{name}: no {lang} sources to scan"
            for lang in ("ts", "py")
            if lang in _obj(packages.get(name)) and lang not in files
        ]
        errs += [
            f"decisions packages.{name}.{lang}: list {home}, the package's own sources, so a "
            "new file there is scanned"
            for lang, home in _homes(api, name, root).items()
            if home not in _list(files.get(lang))
        ]
        errs += scan_problems(name, files, factories, root)
    return errs


def _homes(api: Json, name: str, root: pathlib.Path) -> dict[str, str]:
    """Where a package's own sources live, per language: its TS package's src directory, and
    its Python module (a file or a package directory). A package that lives inside core has no
    home of its own; it lists its files."""
    packages = _obj(_obj(api).get("packages"))
    core, package = _obj(packages.get("core")), _obj(packages.get(name))
    homes: dict[str, str] = {}
    ts, py = package.get("ts"), package.get("py")
    if isinstance(ts, str) and ts != core.get("ts"):
        homes["ts"] = f"typescript/packages/{ts.rpartition('/')[2]}/src"
    if isinstance(py, str) and py != core.get("py"):
        base = "python/src/" + py.replace(".", "/")
        homes["py"] = f"{base}.py" if (root / f"{base}.py").is_file() else base
    return homes


def check_decisions(api: Json, doc: Json, root: pathlib.Path) -> list[str]:
    errs = _shape(doc)
    return errs or [*_factory_problems(api, doc), *_scan(api, doc, root)]


def check_factory_contract(api: Json, meta: Json, spec: pathlib.Path) -> list[str]:
    decisions: Json = json.loads((spec / "api-surface-factory-decisions.json").read_text())
    return [*check_factories(api, meta), *check_decisions(api, decisions, spec.parent)]

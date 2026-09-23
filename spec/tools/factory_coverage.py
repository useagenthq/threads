# pyright: strict
"""Test evidence a provider factory owes the coverage registry (spec/api-coverage.json), beyond
lane 01's functions, required methods and required options:

- `<factory>!<code>`: a passing refusal test per language each expanded `config_errors` entry
  applies to (the `<factory>!<code>@<lang>` of lane 15; the language is the registry list key);
- `<factory>.<option>=default`: a passing behavior test per language for each defaulted param
  that spec/api-surface-factory-decisions.json doesn't list as review-only.

Stdlib only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api_factories import expanded_errors, langs

if TYPE_CHECKING:
    from api_factories import Json


def _obj(v: Json | None) -> dict[str, Json]:
    return v if isinstance(v, dict) else {}


def _list(v: Json | None) -> list[Json]:
    return v if isinstance(v, list) else []


def factory_evidence(api: Json, decisions: Json) -> dict[str, frozenset[str]]:
    """Registry name -> the languages that need a passing test for it."""
    packages = _obj(_obj(api).get("packages"))
    review = _obj(_obj(decisions).get("factories"))
    out: dict[str, set[str]] = {}
    for name, f in ((k, _obj(v)) for k, v in _obj(_obj(api).get("functions")).items()):
        if _obj(packages.get(str(f.get("package", "core")))).get("kind") != "adapter":
            continue
        for code, lang in expanded_errors(f):
            out.setdefault(f"{name}!{code}", set()).add(lang)
        skip = _obj(_obj(review.get(name)).get("review_only"))
        for p in (_obj(x) for x in _list(f.get("params"))):
            pname = str(p.get("name"))
            if ("default" in p or "default_doc" in p) and pname not in skip:
                applies = set(langs(p)) & set(langs(f))
                out.setdefault(f"{name}.{pname}=default", set()).update(applies)
    return {k: frozenset(v) for k, v in out.items()}

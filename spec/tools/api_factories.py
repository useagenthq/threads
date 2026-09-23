# pyright: strict
"""spec/api.json's package and factory rules, beyond what api.schema.json can say.

- every package names at least one language, and every function's package exists;
- a function of a one-language package carries that `lang`;
- `config_errors` codes are ConfigErrorCode members (api.schema.json mirrors the enum, and the
  mirror must match), an entry's `lang` is a language the callable exists in and is written only
  when the callable exists in both, and no code is listed twice for one language;
- a "Seam:" param (a transport or clock swapped in tests) exists in one language, so it has `lang`.

An entry without `lang` applies to every language the callable exists in (`expanded_errors`):
the checker, the refusal tests and the reference all read it that way. Stdlib only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None

LANGS = ("py", "ts")


def _obj(v: Json | None) -> dict[str, Json]:
    return v if isinstance(v, dict) else {}


def _list(v: Json | None) -> list[Json]:
    return v if isinstance(v, list) else []


def langs(node: dict[str, Json]) -> tuple[str, ...]:
    """The languages a callable or param exists in: its `lang`, or both."""
    lang = node.get("lang")
    return (lang,) if isinstance(lang, str) else LANGS


def expanded_errors(f: dict[str, Json]) -> list[tuple[str, str]]:
    """(code, language) for each config_errors entry, sorted: no `lang` means every language the
    callable exists in."""
    out: list[tuple[str, str]] = []
    for e in _list(f.get("config_errors")):
        entry = _obj(e)
        out += [(str(entry.get("code")), lang) for lang in langs(entry) if lang in langs(f)]
    return sorted(out)


def _functions(api: Json) -> Iterator[tuple[str, dict[str, Json]]]:
    for key, f in _obj(_obj(api).get("functions")).items():
        yield key, _obj(f)


def _package_problems(api: Json) -> list[str]:
    packages = _obj(_obj(api).get("packages"))
    errs = [
        f"api.json packages.{k}: needs ts, py or both"
        for k, p in packages.items()
        if not any(lang in _obj(p) for lang in LANGS)
    ]
    for key, f in _functions(api):
        name = str(f.get("package", "core"))
        package = packages.get(name)
        if package is None:
            errs.append(f"api.json {key}: package {name} is not in packages")
            continue
        present = tuple(lang for lang in LANGS if lang in _obj(package))
        if len(present) == 1 and langs(f) != present:
            errs.append(
                f"api.json {key}: package {name} exists only in {present[0]}; "
                f"add lang: {present[0]}"
            )
    return errs


def _error_problems(key: str, f: dict[str, Json], codes: list[Json]) -> list[str]:
    errs: list[str] = []
    for e in _list(f.get("config_errors")):
        entry = _obj(e)
        code, lang = entry.get("code"), entry.get("lang")
        if code not in codes:
            errs.append(f"api.json {key}: config_errors code {code} is not a ConfigErrorCode")
        if lang is None:
            continue
        if lang not in langs(f):
            errs.append(
                f"api.json {key}: config_errors {code} has lang {lang}, "
                f"but {key} exists only in {langs(f)[0]}"
            )
        elif len(langs(f)) == 1:
            errs.append(
                f"api.json {key}: config_errors {code}: {key} exists only in {lang}; drop lang"
            )
    expanded = expanded_errors(f)
    errs += [
        f"api.json {key}: config_errors lists {code} twice for {lang}"
        for code, lang in sorted(set(expanded))
        if expanded.count((code, lang)) > 1
    ]
    return errs


def _seam_problems(key: str, f: dict[str, Json]) -> list[str]:
    return [
        f"api.json {key}.{p.get('name')}: a Seam: param exists for tests in one language; add lang"
        for p in (_obj(x) for x in _list(f.get("params")))
        if str(p.get("doc", "")).startswith("Seam:") and "lang" not in p
    ]


def check_factories(api: Json, meta: Json) -> list[str]:
    codes = _list(
        _obj(_obj(_obj(_obj(api).get("types")).get("ConfigErrorCode")).get("type")).get("enum")
    )
    mirror = _list(_obj(_obj(_obj(meta).get("$defs")).get("ConfigErrorCode")).get("enum"))
    errs = _package_problems(api)
    if mirror != codes:
        errs.append(
            "api.schema.json $defs/ConfigErrorCode must equal api.json types.ConfigErrorCode"
        )
    for key, f in _functions(api):
        errs += _error_problems(key, f, codes)
        errs += _seam_problems(key, f)
    return errs

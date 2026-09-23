# pyright: strict
"""The refusal source scan for adapter packages (lane 15).

Each package in spec/api-surface-factory-decisions.json lists its sources per language: files, or
directories scanned whole (every .ts or .py below them), so a new file in a listed directory is
audited without a list change. Every ConfigError code a source can raise, directly or through a
core helper that raises one (HELPERS), must be in the expanded `config_errors` of each of the
package's factories in that language. A code the owning lane will declare later is a temporary
gap that names that lane and pins its sites (file → count), so a new site of the same code is
red, and the gap is red once any factory declares the code. A construction whose code isn't a
string literal, and any other use of ConfigError than constructing, importing or catching it
by name (an alias, a subclass, a tuple), is always red, so none escapes the count. Stdlib only.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from api_factories import expanded_errors, langs
from config_error_uses import CALLS, HELPERS, unclassified_uses, without_comments

if TYPE_CHECKING:
    import pathlib

    from api_factories import Json

# A construction whose code the scan can read: a string literal first, positional or code=.
CONSTRUCTED = re.compile(r"ConfigError\(\s*(?:code\s*=\s*)?[\"'](\w+)[\"']")
ANY_CONSTRUCTION = re.compile(r"\bConfigError\(")
UNREADABLE = "<unreadable>"
SUFFIX = {"ts": ".ts", "py": ".py"}


def _obj(v: Json | None) -> dict[str, Json]:
    return v if isinstance(v, dict) else {}


def _list(v: Json | None) -> list[Json]:
    return v if isinstance(v, list) else []


def raised_codes(source: str, lang: str) -> Counter[str]:
    """How many sites in one source can raise each ConfigError code. A construction whose code
    isn't a string literal, and any mention of ConfigError other than a construction, a plain
    import or a catch (config_error_uses.py), counts as UNREADABLE, which the scan reports."""
    # A Python comment is never a site; TS text is counted whole (a comment over-counts, which is
    # red, never hidden).
    code_text = without_comments(source) if lang == "py" else source
    found: Counter[str] = Counter(m.group(1) for m in CONSTRUCTED.finditer(code_text))
    unreadable = len(ANY_CONSTRUCTION.findall(code_text)) - found.total()
    unreadable += unclassified_uses(source, lang)
    if unreadable:
        found[UNREADABLE] = unreadable
    for helper, code in HELPERS[lang].items():
        found[code] += len(CALLS[lang][helper].findall(code_text))
    return +found


def source_shape(name: str, p: Json) -> list[str]:
    """A package's source lists and gaps are well formed (a malformed list would scan nothing)."""
    entry = _obj(p)
    errs: list[str] = []
    present = [k for k in ("ts", "py") if k in entry]
    if not present or not set(entry) <= {"ts", "py", "gaps"}:
        errs.append(f"decisions packages.{name}: expected {{ts?, py?}} source lists and gaps?")
    for lang in present:
        paths = _list(entry[lang])
        if not paths or not all(isinstance(x, str) and x for x in paths):
            errs.append(f"decisions packages.{name}.{lang}: expected a non-empty list of paths")
    for code, gap in _obj(entry.get("gaps")).items():
        g = _obj(gap)
        sites = _obj(g.get("sites"))
        ok = set(g) == {"owner", "why", "sites"} and all(
            isinstance(g[k], str) for k in ("owner", "why")
        )
        counts = all(isinstance(n, int) and n > 0 for n in sites.values())
        if not ok or not sites or not counts:
            errs.append(
                f"decisions packages.{name}.gaps.{code}: expected {{owner, why, sites: "
                "{path: count}}"
            )
    return errs


def _files(root: pathlib.Path, path: str, lang: str) -> list[pathlib.Path] | None:
    target = root / path
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(target.rglob(f"*{SUFFIX[lang]}"))
    return None


def _package_codes(
    root: pathlib.Path, name: str, paths: list[Json], lang: str
) -> tuple[dict[str, Counter[str]], list[str]]:
    """code -> {file (relative to root) -> sites}, and missing-path problems."""
    by_code: dict[str, Counter[str]] = {}
    errs: list[str] = []
    for path in (str(p) for p in paths):
        files = _files(root, path, lang)
        if files is None:
            errs.append(f"decisions packages.{name}: {path} does not exist")
            continue
        for file in files:
            rel = file.relative_to(root).as_posix()
            for code, n in raised_codes(file.read_text(), lang).items():
                by_code.setdefault(code, Counter())[rel] += n
    return by_code, errs


def _gap_problems(name: str, code: str, gap: dict[str, Json], found: Counter[str]) -> list[str]:
    allowed = {k: v for k, v in _obj(gap.get("sites")).items() if isinstance(v, int)}
    if dict(found) == allowed:
        return []
    return [
        f"decisions packages.{name}.gaps.{code}: sites {dict(sorted(found.items()))} != the "
        f"gap's {allowed}; declare {code} in config_errors or update the gap"
    ]


def scan_problems(
    name: str, sources: dict[str, Json], factories: dict[str, dict[str, Json]], root: pathlib.Path
) -> list[str]:
    """One adapter package: every scanned code is declared or a pinned gap."""
    gaps = _obj(sources.get("gaps"))
    declared = {k: set(expanded_errors(f)) for k, f in factories.items()}
    errs = [
        f"decisions packages.{name}.gaps.{code}: {k} declares it now; drop the gap"
        for code in sorted(gaps)
        for k, codes in declared.items()
        if any(c == code for c, _ in codes)
    ]
    seen: dict[str, Counter[str]] = {}
    for lang in ("ts", "py"):
        by_code, missing = _package_codes(root, name, _list(sources.get(lang)), lang)
        errs += missing
        for code, found in sorted(by_code.items()):
            if code == UNREADABLE:
                errs.append(
                    f"decisions packages.{name}: {', '.join(sorted(found))} uses ConfigError or "
                    "a refusing helper in a way the scan can't count; only call them (ConfigError "
                    "with its code as a string literal), import or catch them by name"
                )
                continue
            if code in gaps:
                seen.setdefault(code, Counter()).update(found)
                continue
            errs += [
                f"api.json {k}: {name} sources can raise {code} ({lang}, "
                f"{', '.join(sorted(found))}); declare it in config_errors"
                for k, codes in declared.items()
                if lang in langs(factories[k]) and (code, lang) not in codes
            ]
    for code, gap in sorted(gaps.items()):
        errs += _gap_problems(name, code, _obj(gap), seen.get(code, Counter()))
    return errs

# pyright: strict
"""Reference tool_search matching (spec/schema/README.md, "tool_search"): exact names first, then
keyword ranking, else no match."""

from __future__ import annotations

from dataclasses import dataclass

from .ucd import fold, terms, tokens

NO_MATCH = "no deferred tool matches"
MAX_EXACT = 10
MAX_QUERY = 200
"""Code points."""


@dataclass(frozen=True, slots=True)
class Found:
    lines: list[str]
    """The result text's lines, in result order."""
    loaded: list[str]
    """The deferred tools this search loads, in result order."""


def _first_line(description: str) -> str:
    return description.split("\n", 1)[0]


def search(query: str, limit: int, deferred: list[tuple[str, str]], loaded: list[str]) -> Found:
    """`deferred`: (name, description) of every still-deferred tool, in tool-set order.
    `loaded`: every other current tool's name."""
    exact = _exact(query, deferred, loaded)
    if exact is not None:
        return exact
    ranked = _ranked(query, limit, deferred)
    if ranked:
        return Found([f"{n}: {_first_line(d)}" for n, d in ranked], [n for n, _ in ranked])
    return Found([NO_MATCH], [])


def _exact(query: str, deferred: list[tuple[str, str]], loaded: list[str]) -> Found | None:
    names = {fold(n): n for n, _ in deferred} | {fold(n): n for n in loaded}
    found = terms(query)
    if not found or any(fold(t) not in names for t in found):
        return None
    described = dict(deferred)
    lines: list[str] = []
    load: list[str] = []
    seen: set[str] = set()
    for t in found:
        name = names[fold(t)]
        if name in seen:
            continue
        seen.add(name)
        if name not in described:
            lines.append(f"{name}: already loaded")
        elif len(load) < MAX_EXACT:
            load.append(name)
            lines.append(f"{name}: {_first_line(described[name])}")
    return Found(lines, load)


def _ranked(query: str, limit: int, deferred: list[tuple[str, str]]) -> list[tuple[str, str]]:
    wanted = set(tokens(query))
    scored = [(len(wanted & set(tokens(f"{n} {d}"))), n, d) for n, d in deferred]
    kept = sorted(((-s, n, d) for s, n, d in scored if s > 0), key=lambda x: (x[0], _cp(x[1])))
    return [(n, d) for _, n, d in kept[:limit]]


def _cp(s: str) -> list[int]:
    """Code-point order (Python's str order already is; spelled out for the reader)."""
    return [ord(c) for c in s]

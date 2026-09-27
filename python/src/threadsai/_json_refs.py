"""`$ref` checks for semantic rule 20 (spec/schema/README.md, Output schemas): every reference
names `#` or an existing `$defs` entry, and no chain of references comes back to where it
started without reaching into the value (`{"$ref": "#"}` would check the same value forever).
validate/refs.ts is the TypeScript side."""

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import JsonValue

_DEFS: Final = "#/$defs/"
_SAME_VALUE: Final = ("not", "if", "then", "else")
"""Keywords whose subschema checks the same value, not a part of it."""
_SAME_VALUE_LISTS: Final = ("allOf", "anyOf", "oneOf")


def check_refs(schema: Mapping[str, JsonValue]) -> None:
    """Raises TypeError for a reference to nothing, or a reference cycle that consumes no
    input."""
    defs = schema.get("$defs", {})
    if not isinstance(defs, dict):
        raise TypeError("$defs is not a map of schemas")
    graph = {"#": _refs(dict(schema))} | {_DEFS + name: _refs(d) for name, d in defs.items()}
    missing = sorted(_every_ref(dict(schema)) - graph.keys())
    if missing:
        raise TypeError(f"$ref to a missing definition {missing[0]!r}")
    done: set[str] = set()
    for start in graph:
        _no_cycle(graph, start, (), done)


def _every_ref(node: JsonValue) -> frozenset[str]:
    """Every reference anywhere in the schema."""
    if isinstance(node, list):
        return frozenset[str]().union(*(_every_ref(n) for n in node))
    if not isinstance(node, dict):
        return frozenset()
    ref = node.get("$ref")
    own = frozenset({ref}) if isinstance(ref, str) else frozenset[str]()
    return own.union(*(_every_ref(v) for v in node.values()))


def _refs(schema: JsonValue) -> frozenset[str]:
    """The references `schema` follows while still checking the same value."""
    found: set[str] = set()
    stack: list[JsonValue] = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref)
        stack.extend(node[k] for k in _SAME_VALUE if k in node)
        for key in _SAME_VALUE_LISTS:
            listed = node.get(key)
            if isinstance(listed, list):
                stack.extend(listed)
    return frozenset(found)


def _no_cycle(
    graph: Mapping[str, frozenset[str]], at: str, path: Sequence[str], done: set[str]
) -> None:
    """Depth first from `at`; `done` holds the nodes already proven to lead to no cycle."""
    if at in done:
        return
    if at in path:
        raise TypeError(f"$refs loop without reaching into the value: {' -> '.join((*path, at))}")
    for target in sorted(graph[at]):
        _no_cycle(graph, target, (*path, at), done)
    done.add(at)

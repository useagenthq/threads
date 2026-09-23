# pyright: strict
"""Which spec/api.json entries have an explanation, and what it is.

One rule, shared by check_api.py (the gate) and the docs generator (the rows), so the two can't
disagree. Stdlib only.

- Every function, type, method, parameter, option, field and property, and every field of an
  inline object and parameter of a callback at any depth, needs an explanation.
- An entry's own `doc` is its explanation.
- A field or property whose type references a type (through array, map, partial, promise, stream
  or result) may instead take the first sentence of that type's doc, or of the wire schema
  definition's description. Inputs (parameters, options, inline fields, callback parameters)
  never inherit: a type says what a value is, not what this call does with it.
- An optional input without a literal `default` says in its doc what omitting it does.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None
type Node = Mapping[str, Json]
type Kind = Literal[
    "function",
    "type",
    "method",
    "param",
    "option",
    "field",
    "property",
    "inline_field",
    "callback_param",
]

# Inputs a caller writes, and may leave out when they are optional.
WRITTEN: frozenset[Kind] = frozenset({"param", "option", "inline_field"})
# Wrappers a field can inherit through: the value is still "one of" the referenced type.
REF_WRAPPERS = ("array", "map", "partial", "promise", "stream", "result")
# Wrappers a nested input can hide behind, where a caller still writes its fields.
NESTING = (*REF_WRAPPERS, "schema", "type")
OMISSION = re.compile(r"\b(omit|omitted|omitting|default|defaults)\b", re.IGNORECASE)


class Entry(NamedTuple):
    """One public contract entry: its api.json node, what kind of entry it is, and where."""

    node: Node
    kind: Kind
    path: str


def _obj(v: Json | None) -> dict[str, Json]:
    return v if isinstance(v, dict) else {}


def _list(v: Json | None) -> list[Json]:
    return v if isinstance(v, list) else []


def first_sentence(text: str) -> str:
    match = re.match(r"(.+?[.!?])(\s|$)", text.strip(), re.DOTALL)
    return match.group(1) if match else text.strip()


def walk(api: Json) -> Iterator[Entry]:
    """Every public entry of the contract, parents before their children."""
    root = _obj(api)
    for key, f in _obj(root.get("functions")).items():
        yield Entry(_obj(f), "function", key)
        yield from _params(_obj(f), key, callback=False)
    for name, t in _obj(root.get("types")).items():
        yield from _type(name, _obj(t))


def walk_type(t: Json, path: str) -> Iterator[Entry]:
    """The inline object fields and callback parameters inside one type expression."""
    node = _obj(t)
    if "object" in node:
        for key, field in _obj(node["object"]).items():
            yield Entry(_obj(field), "inline_field", f"{path}.{key}")
            yield from walk_type(_obj(field).get("type"), f"{path}.{key}")
    elif "fn" in node:
        yield from _params(_obj(node["fn"]), path, callback=True)
    elif "union" in node:
        for alt in _list(node["union"]):
            yield from walk_type(alt, path)
    else:
        for k in NESTING:
            if k in node:
                yield from walk_type(node[k], path)


def _params(f: Node, path: str, *, callback: bool) -> Iterator[Entry]:
    for p in _list(f.get("params")):
        param = _obj(p)
        kind: Kind = "callback_param" if callback else _param_kind(param)
        at = f"{path}.{param.get('name')}"
        yield Entry(param, kind, at)
        yield from walk_type(param.get("type"), at)


def _param_kind(param: Node) -> Kind:
    return "option" if param.get("kind") == "option" else "param"


def _type(name: str, t: Node) -> Iterator[Entry]:
    yield Entry(t, "type", name)
    yield from _fields(t.get("fields"), name, "field")
    disc = str(t.get("discriminator"))
    for variant in _list(t.get("variants")):
        fields = _obj(_obj(variant).get("object"))
        tag = _obj(_obj(fields.get(disc)).get("type")).get("literal")
        yield from _fields(fields, f"{name}.{tag}", "field")
    if t.get("kind") == "alias":
        yield from walk_type(t.get("type"), name)
    yield from _fields(t.get("properties"), name, "property")
    for key, m in _obj(t.get("methods")).items():
        yield Entry(_obj(m), "method", f"{name}.{key}")
        yield from _params(_obj(m), f"{name}.{key}", callback=False)


def _fields(fields: Json | None, path: str, kind: Kind) -> Iterator[Entry]:
    for key, field in _obj(fields).items():
        yield Entry(_obj(field), kind, f"{path}.{key}")
        yield from walk_type(_obj(field).get("type"), f"{path}.{key}")


def _referenced(t: Json | None) -> str | None:
    """The $ref a field's type names, looking through wrappers only (never through unions)."""
    node = _obj(t)
    ref = node.get("$ref")
    if isinstance(ref, str):
        return ref
    for k in REF_WRAPPERS:
        if k in node:
            return _referenced(node[k])
    return None


def _pointer(doc: Json | None, frag: str) -> Json | None:
    node = doc
    for raw in frag.split("/")[1:] if frag else []:
        node = _obj(node).get(raw.replace("~1", "/").replace("~0", "~"))
    return node


def _inherited(ref: str, types: Node, schemas: Mapping[str, Json]) -> str | None:
    base, _, frag = ref.partition("#")
    if not base:
        target = _obj(types.get(frag.removeprefix("/types/")))
        text = target.get("doc")
    else:
        if frag.endswith("/JsonValue"):
            return None
        text = _obj(_pointer(schemas.get(base), frag)).get("description")
    if not isinstance(text, str) or not text.strip():
        return None
    return first_sentence(text)


def resolved_doc(entry: Entry, types: Node, schemas: Mapping[str, Json]) -> str | None:
    """The entry's explanation: its own doc, else (fields and properties only) the first sentence
    of the type it references. None means it has none."""
    own = entry.node.get("doc")
    if isinstance(own, str) and own.strip():
        return own.strip()
    if entry.kind not in ("field", "property"):
        return None
    ref = _referenced(entry.node.get("type"))
    return None if ref is None else _inherited(ref, types, schemas)


def _problem(entry: Entry, types: Node, schemas: Mapping[str, Json]) -> str | None:
    doc = resolved_doc(entry, types, schemas)
    if doc is None:
        return "no doc"
    optional = entry.kind in WRITTEN and entry.node.get("required") is False
    if optional and "default" not in entry.node and not OMISSION.search(doc):
        return "optional with no default: say what omitting it does"
    return None


def check_docs(api: Json, schemas: Mapping[str, Json]) -> list[str]:
    """One line per entry without an explanation."""
    types = _obj(_obj(api).get("types"))
    return [
        f"api.json {e.path} ({e.kind}): {why}"
        for e in walk(api)
        if (why := _problem(e, types, schemas)) is not None
    ]

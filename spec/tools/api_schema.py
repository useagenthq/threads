# pyright: strict
"""JSON helpers and the draft 2020-12 subset that validates spec/api.json against
spec/schema/api.schema.json. Any keyword outside the subset is itself an error, so the subset
stays honest. Stdlib only; used by check_api.py and api_host.py."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None

IGNORED = frozenset({"$schema", "$id", "$defs", "$comment", "title", "description", "default"})
TYPES: dict[str, Callable[[Json], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def obj(v: Json) -> dict[str, Json]:
    return v if isinstance(v, dict) else {}


def arr(v: Json) -> list[Json]:
    return v if isinstance(v, list) else []


def strs(v: Json) -> list[str]:
    return [x for x in arr(v) if isinstance(x, str)]


def pointer(doc: Json, frag: str) -> Json | None:
    node: Json | None = doc
    for raw in frag.split("/")[1:] if frag else []:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    return node


class Validator:
    """Draft 2020-12 subset, enough for api.schema.json. $ref resolves inside the meta-schema."""

    def __init__(self, root: Json) -> None:
        self.root = root
        self.handlers: dict[str, Callable[[dict[str, Json], Json, Json, str], list[str]]] = {
            "$ref": self._ref, "type": self._type, "properties": self._properties,
            "required": self._required, "additionalProperties": self._additional,
            "propertyNames": self._names, "items": self._items, "enum": self._enum,
            "const": self._const, "oneOf": self._one_of, "anyOf": self._any_of,
            "pattern": self._pattern, "minLength": self._min_length,
            "minItems": self._min_items, "minProperties": self._min_properties,
        }  # fmt: skip

    def check(self, schema: Json, v: Json, at: str) -> list[str]:
        errs: list[str] = []
        for k, arg in obj(schema).items():
            if k in IGNORED:
                continue
            handler = self.handlers.get(k)
            if handler is None:
                errs.append(f"api.schema.json: unsupported keyword {k}")
            else:
                errs += handler(obj(schema), arg, v, at)
        return errs

    def _ref(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return self.check(pointer(self.root, str(arg).partition("#")[2]), v, at)

    def _type(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [] if TYPES[str(arg)](v) else [f"{at}: expected {arg}"]

    def _properties(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        if not isinstance(v, dict):
            return []
        props = obj(arg)
        return [e for k, x in v.items() if k in props for e in self.check(props[k], x, f"{at}/{k}")]

    def _required(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        if not isinstance(v, dict):
            return []
        return [f"{at}: missing {k}" for k in strs(arg) if k not in v]

    def _additional(self, s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        if not isinstance(v, dict):
            return []
        extra = [k for k in v if k not in obj(s.get("properties"))]
        if arg is False:
            return [f"{at}: unexpected {k}" for k in extra]
        return [e for k in extra for e in self.check(arg, v[k], f"{at}/{k}")]

    def _names(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [e for k in obj(v) for e in self.check(arg, k, f"{at}/{k} (name)")]

    def _items(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [e for i, x in enumerate(arr(v)) for e in self.check(arg, x, f"{at}/{i}")]

    def _enum(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [] if v in arr(arg) else [f"{at}: {v!r} not in {arg}"]

    def _const(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [] if v == arg and type(v) is type(arg) else [f"{at}: expected {arg!r}"]

    def _one_of(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        results = [self.check(alt, v, at) for alt in arr(arg)]
        passed = sum(1 for r in results if not r)
        if passed == 1:
            return []
        if passed > 1:
            return [f"{at}: matches {passed} oneOf alternatives"]
        return min(results, key=len)

    def _any_of(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        results = [self.check(alt, v, at) for alt in arr(arg)]
        return [] if any(not r for r in results) else min(results, key=len)

    def _pattern(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        ok = not isinstance(v, str) or re.search(str(arg), v)
        return [] if ok else [f"{at}: {v!r} does not match {arg}"]

    def _min_length(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        ok = not isinstance(v, str) or len(v) >= int(str(arg))
        return [] if ok else [f"{at}: shorter than {arg}"]

    def _min_items(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        ok = not isinstance(v, list) or len(v) >= int(str(arg))
        return [] if ok else [f"{at}: fewer than {arg} items"]

    def _min_properties(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        ok = not isinstance(v, dict) or len(v) >= int(str(arg))
        return [] if ok else [f"{at}: fewer than {arg} properties"]

"""JSON read from spec/ files, narrowed to the shape the caller expects."""

import json
from pathlib import Path

type Json = str | int | float | bool | list[Json] | dict[str, Json] | None
type Obj = dict[str, Json]


def load(path: Path) -> Json:
    return json.loads(path.read_text())


def obj(value: Json) -> Obj:
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected an object, got {value!r}")


def array(value: Json) -> list[Json]:
    if isinstance(value, list):
        return value
    raise TypeError(f"expected an array, got {value!r}")


def objs(value: Json) -> list[Obj]:
    return [obj(v) for v in array(value)]


def text(value: Json) -> str:
    if isinstance(value, str):
        return value
    raise TypeError(f"expected a string, got {value!r}")


def doc_of(node: Obj) -> str:
    """The node's doc prose, or "" when it has none."""
    return text(node.get("doc", ""))

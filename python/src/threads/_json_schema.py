"""The JSON Schema evaluator for the keywords threads checks at runtime: the cross-field rules the
model generator leaves to `StrictModel`, and pinned output schemas (semantic rule 20), which are
what a Pydantic model writes: constraints, enums, nested and recursive models by `$ref`.

`holds` raises TypeError on a keyword it can't check rather than skipping it: a rule that can't be
checked must never pass silently. `unchecked` names such a keyword anywhere in a schema, so an
output model that uses one is refused at setup instead of failing a run.
"""

import operator
import re
from collections.abc import Callable, Mapping, Sequence, Sized
from typing import Final, TypeGuard

from pydantic import JsonValue

type Defs = Mapping[str, JsonValue]
type _Check = Callable[[JsonValue, object, Defs], bool]

_ANNOTATIONS: Final = frozenset({"description", "title", "default", "examples", "$defs"})
"""Keywords that describe a value and never constrain it (a Pydantic schema writes them)."""
_DEFS: Final = "#/$defs/"


def holds(schema: JsonValue, value: object) -> bool:
    """Whether `value` satisfies `schema`. `$ref`s name the root's `$defs`."""
    root: JsonValue = schema.get("$defs", {}) if isinstance(schema, dict) else {}
    if not isinstance(root, dict):
        raise TypeError("$defs is not a map of schemas")
    return _holds(schema, value, root)


def _holds(schema: JsonValue, value: object, defs: Defs) -> bool:
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        raise TypeError(f"not a schema: {schema!r}")
    if "if" in schema:
        branch = schema.get("then") if _holds(schema["if"], value, defs) else schema.get("else")
        if branch is not None and not _holds(branch, value, defs):
            return False
    if "additionalProperties" in schema and not _additional(schema, value, defs):
        return False
    for keyword, argument in schema.items():
        if keyword in ("if", "then", "else", "additionalProperties") or keyword in _ANNOTATIONS:
            continue
        check = _CHECKS.get(keyword)
        if check is None:
            raise TypeError(f"unsupported schema keyword {keyword!r}")
        if not check(argument, value, defs):
            return False
    return True


def unchecked(schema: JsonValue) -> str | None:
    """The first keyword `holds` can't check anywhere in `schema` (or a `$ref` it can't
    follow, or a pattern it can't compile), else None."""
    try:
        _walk(schema)
    except TypeError as error:
        return str(error)
    return None


_SCHEMA_ARG: Final = frozenset({"not", "if", "then", "else", "items", "additionalProperties"})
_LIST_ARG: Final = frozenset({"allOf", "anyOf", "oneOf"})
_MAP_ARG: Final = frozenset({"properties", "$defs"})


def _walk(schema: JsonValue) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise TypeError(f"not a schema: {schema!r}")
    for keyword, argument in schema.items():
        _admit(keyword, argument)
        for sub in _subschemas(keyword, argument):
            _walk(sub)


def _admit(keyword: str, argument: JsonValue) -> None:
    """Raises TypeError for a keyword `holds` can't check."""
    if keyword == "pattern":
        _pattern(argument)
    elif keyword == "$ref":
        _name(argument)
    elif keyword not in _CHECKS and keyword not in _ANNOTATIONS and keyword not in _SCHEMA_ARG:
        raise TypeError(f"unsupported schema keyword {keyword!r}")


def _subschemas(keyword: str, argument: JsonValue) -> Sequence[JsonValue]:
    if keyword in _SCHEMA_ARG:
        return (argument,)
    if keyword in _LIST_ARG:
        return _schemas(argument)
    if keyword in _MAP_ARG:
        return tuple(_map(argument).values())
    return ()


def is_object(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, dict)


def _is_array(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, list)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _schemas(argument: JsonValue) -> Sequence[JsonValue]:
    if not isinstance(argument, list):
        raise TypeError(f"expected a list of schemas: {argument!r}")
    return argument


def _map(argument: JsonValue) -> Mapping[str, JsonValue]:
    if not isinstance(argument, dict):
        raise TypeError(f"expected a map of schemas: {argument!r}")
    return argument


def json_equal(expected: JsonValue, value: object) -> bool:
    # JSON true is not 1 and 1.0 is not the integer 1, though Python's
    # True == 1 == 1.0.
    return type(expected) is type(value) and expected == value


def _name(ref: JsonValue) -> str:
    """The `$defs` entry a local reference names; any other reference can't be followed."""
    if not isinstance(ref, str) or not ref.startswith(_DEFS):
        raise TypeError(f"unsupported $ref {ref!r}")
    return ref.removeprefix(_DEFS)


def _ref(argument: JsonValue, value: object, defs: Defs) -> bool:
    name = _name(argument)
    if name not in defs:
        raise TypeError(f"$ref to a missing definition {name!r}")
    return _holds(defs[name], value, defs)


def _pattern(argument: JsonValue) -> re.Pattern[str]:
    # ECMA-262 without the u flag: \d, \w and \s are ASCII.
    if not isinstance(argument, str):
        raise TypeError(f"a pattern is a string: {argument!r}")
    try:
        return re.compile(argument, re.ASCII)
    except re.error as error:
        raise TypeError(f"pattern {argument!r} doesn't compile: {error}") from None


def _matches(argument: JsonValue, value: object, _defs: Defs) -> bool:
    return not isinstance(value, str) or _pattern(argument).search(value) is not None


def _required(argument: JsonValue, value: object, _defs: Defs) -> bool:
    return not is_object(value) or all(key in value for key in _schemas(argument))


def _properties(argument: JsonValue, value: object, defs: Defs) -> bool:
    return not is_object(value) or all(
        _holds(sub, value[key], defs) for key, sub in _map(argument).items() if key in value
    )


def _additional(schema: dict[str, JsonValue], value: object, defs: Defs) -> bool:
    if not is_object(value):
        return True
    declared = _map(schema.get("properties", {}))
    extra = schema["additionalProperties"]
    return all(_holds(extra, value[key], defs) for key in value if key not in declared)


def _items(argument: JsonValue, value: object, defs: Defs) -> bool:
    return not _is_array(value) or all(_holds(argument, item, defs) for item in value)


def _is_integer(value: object) -> bool:
    if isinstance(value, float):
        return value.is_integer()
    return isinstance(value, int) and not isinstance(value, bool)


_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": is_object,
    "array": _is_array,
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
    "integer": _is_integer,
    "number": _is_number,
}


def _type(argument: JsonValue, value: object, _defs: Defs) -> bool:
    names = argument if isinstance(argument, list) else [argument]
    for name in names:
        if not isinstance(name, str) or name not in _JSON_TYPES:
            raise TypeError(f"unknown JSON type {name!r}")
    return any(_JSON_TYPES[str(name)](value) for name in names)


type _Compare = Callable[[float, float], bool]


def _bound(compare: _Compare) -> _Check:
    """A bound on a number; any other value passes."""

    def check(argument: JsonValue, value: object, _defs: Defs) -> bool:
        if not _is_number(argument):
            raise TypeError(f"a bound is a number: {argument!r}")
        return not _is_number(value) or compare(value, argument)

    return check


def _size(of: type[Sized], compare: _Compare) -> _Check:
    """A bound on the length of a string (in code points, as JSON Schema counts), an array or
    an object; any other value passes."""

    def check(argument: JsonValue, value: object, _defs: Defs) -> bool:
        if not _is_number(argument):
            raise TypeError(f"a length bound is a number: {argument!r}")
        return not isinstance(value, of) or compare(len(value), argument)

    return check


_CHECKS: Mapping[str, _Check] = {
    "not": lambda argument, value, defs: not _holds(argument, value, defs),
    "allOf": lambda argument, value, defs: all(_holds(s, value, defs) for s in _schemas(argument)),
    "anyOf": lambda argument, value, defs: any(_holds(s, value, defs) for s in _schemas(argument)),
    "oneOf": lambda argument, value, defs: (
        sum(_holds(s, value, defs) for s in _schemas(argument)) == 1
    ),
    "const": lambda argument, value, _defs: json_equal(argument, value),
    "enum": lambda argument, value, _defs: any(json_equal(e, value) for e in _schemas(argument)),
    "required": _required,
    "properties": _properties,
    "type": _type,
    "items": _items,
    "$ref": _ref,
    "pattern": _matches,
    "minimum": _bound(operator.ge),
    "maximum": _bound(operator.le),
    "exclusiveMinimum": _bound(operator.gt),
    "exclusiveMaximum": _bound(operator.lt),
    "minLength": _size(str, operator.ge),
    "maxLength": _size(str, operator.le),
    "minItems": _size(list, operator.ge),
    "maxItems": _size(list, operator.le),
    "minProperties": _size(dict, operator.ge),
}

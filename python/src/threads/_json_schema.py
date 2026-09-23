"""The JSON Schema evaluator for the keywords threads checks at runtime: the cross-field rules the
model generator leaves to `StrictModel`, and pinned output schemas (semantic rule 20), which are
what a Pydantic or Zod schema writes: constraints, formats, enums, nested and recursive
models by `$ref`. spec/schema/README.md defines each keyword; validate/json-schema.ts is the
TypeScript reader of the same definition.

`holds` raises TypeError on a keyword it can't check rather than skipping it: a rule that can't be
checked must never pass silently. `unchecked` names such a keyword anywhere in a schema, so an
output model that uses one is refused at setup instead of failing a run.
"""

import operator
import re
from collections.abc import Callable, Mapping, Sequence, Sized
from typing import Final, TypeGuard

from pydantic import JsonValue

from threads._json_formats import FORMATS, multiple_of

type Root = Mapping[str, JsonValue]
"""The whole schema document: `$ref`s resolve against it."""
type _Check = Callable[[JsonValue, object, Root], bool]

_ANNOTATIONS: Final = frozenset(
    {"description", "title", "default", "examples", "$defs", "$schema", "$comment"}
)
"""Keywords that describe a value and never constrain it."""
_DEFS: Final = "#/$defs/"


def holds(schema: JsonValue, value: object) -> bool:
    """Whether `value` satisfies `schema`. A `$ref` is `#` (the whole schema) or names one of
    its `$defs`."""
    return _holds(schema, value, schema if isinstance(schema, dict) else {})


def _holds(schema: JsonValue, value: object, root: Root) -> bool:
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        raise TypeError(f"not a schema: {schema!r}")
    if "if" in schema:
        branch = schema.get("then") if _holds(schema["if"], value, root) else schema.get("else")
        if branch is not None and not _holds(branch, value, root):
            return False
    if "additionalProperties" in schema and not _additional(schema, value, root):
        return False
    for keyword, argument in schema.items():
        if keyword in ("if", "then", "else", "additionalProperties") or keyword in _ANNOTATIONS:
            continue
        check = _CHECKS.get(keyword)
        if check is None:
            raise TypeError(f"unsupported schema keyword {keyword!r}")
        if not check(argument, value, root):
            return False
    return True


def conforms(schema: Mapping[str, JsonValue], value: JsonValue) -> bool:
    """Semantic rule 20: whether an output value satisfies the pinned output schema. A keyword
    this reader can't check fails closed rather than accepting unchecked output."""
    try:
        return holds(dict(schema), value)
    except TypeError:
        return False


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
    elif keyword == "format" and argument not in FORMATS:
        raise TypeError(f"unsupported format {argument!r}")
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


def _name(ref: JsonValue) -> str | None:
    """The `$defs` entry a local reference names, or None for `#`, the whole schema; any other
    reference can't be followed."""
    if ref == "#":
        return None
    if not isinstance(ref, str) or not ref.startswith(_DEFS):
        raise TypeError(f"unsupported $ref {ref!r}")
    return ref.removeprefix(_DEFS)


def _ref(argument: JsonValue, value: object, root: Root) -> bool:
    name = _name(argument)
    if name is None:
        return _holds(dict(root), value, root)
    defs = _map(root.get("$defs", {}))
    if name not in defs:
        raise TypeError(f"$ref to a missing definition {name!r}")
    return _holds(defs[name], value, root)


def _format(argument: JsonValue, value: object, _root: Root) -> bool:
    check = FORMATS.get(argument) if isinstance(argument, str) else None
    if check is None:
        raise TypeError(f"unsupported format {argument!r}")
    return not isinstance(value, str) or check(value)


def _multiple(argument: JsonValue, value: object, _root: Root) -> bool:
    if not _is_number(argument):
        raise TypeError(f"multipleOf is a number: {argument!r}")
    return not _is_number(value) or multiple_of(value, argument)


def _pattern(argument: JsonValue) -> re.Pattern[str]:
    # ECMA-262 without the u flag: \d, \w and \s are ASCII.
    if not isinstance(argument, str):
        raise TypeError(f"a pattern is a string: {argument!r}")
    try:
        return re.compile(argument, re.ASCII)
    except re.error as error:
        raise TypeError(f"pattern {argument!r} doesn't compile: {error}") from None


def _matches(argument: JsonValue, value: object, _root: Root) -> bool:
    return not isinstance(value, str) or _pattern(argument).search(value) is not None


def _required(argument: JsonValue, value: object, _root: Root) -> bool:
    return not is_object(value) or all(key in value for key in _schemas(argument))


def _properties(argument: JsonValue, value: object, root: Root) -> bool:
    return not is_object(value) or all(
        _holds(sub, value[key], root) for key, sub in _map(argument).items() if key in value
    )


def _additional(schema: dict[str, JsonValue], value: object, root: Root) -> bool:
    if not is_object(value):
        return True
    declared = _map(schema.get("properties", {}))
    extra = schema["additionalProperties"]
    return all(_holds(extra, value[key], root) for key in value if key not in declared)


def _items(argument: JsonValue, value: object, root: Root) -> bool:
    return not _is_array(value) or all(_holds(argument, item, root) for item in value)


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


def _type(argument: JsonValue, value: object, _root: Root) -> bool:
    names = argument if isinstance(argument, list) else [argument]
    for name in names:
        if not isinstance(name, str) or name not in _JSON_TYPES:
            raise TypeError(f"unknown JSON type {name!r}")
    return any(_JSON_TYPES[str(name)](value) for name in names)


type _Compare = Callable[[float, float], bool]


def _bound(compare: _Compare) -> _Check:
    """A bound on a number; any other value passes."""

    def check(argument: JsonValue, value: object, _root: Root) -> bool:
        if not _is_number(argument):
            raise TypeError(f"a bound is a number: {argument!r}")
        return not _is_number(value) or compare(value, argument)

    return check


def _size(of: type[Sized], compare: _Compare) -> _Check:
    """A bound on the length of a string (in code points, as JSON Schema counts), an array or
    an object; any other value passes."""

    def check(argument: JsonValue, value: object, _root: Root) -> bool:
        if not _is_number(argument):
            raise TypeError(f"a length bound is a number: {argument!r}")
        return not isinstance(value, of) or compare(len(value), argument)

    return check


_CHECKS: Mapping[str, _Check] = {
    "not": lambda argument, value, root: not _holds(argument, value, root),
    "allOf": lambda argument, value, root: all(_holds(s, value, root) for s in _schemas(argument)),
    "anyOf": lambda argument, value, root: any(_holds(s, value, root) for s in _schemas(argument)),
    "oneOf": lambda argument, value, root: (
        sum(_holds(s, value, root) for s in _schemas(argument)) == 1
    ),
    "const": lambda argument, value, _root: json_equal(argument, value),
    "enum": lambda argument, value, _root: any(json_equal(e, value) for e in _schemas(argument)),
    "required": _required,
    "properties": _properties,
    "type": _type,
    "items": _items,
    "$ref": _ref,
    "pattern": _matches,
    "format": _format,
    "multipleOf": _multiple,
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

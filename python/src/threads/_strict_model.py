"""Base class of every generated boundary model.

Pydantic types can hold most of the schema, but not cross-field rules (`if`/`then`/`else`,
`not`, `oneOf` over `required`, `minProperties`). The generator copies those rules verbatim from
the schema into each model's `json_schema_extra["allOf"]`, and `StrictModel` checks them against
the raw input before field validation. The rules are data from the schema, never code, so they
can't drift from the TypeScript side.

It also rejects an explicitly passed `MISSING` sentinel, and checks `Literal` fields by exact
JSON type: Pydantic's literal validator lets `true` pass for `Literal[1]` and `1` for
`Literal[True]`, even in strict mode.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from types import UnionType
from typing import Annotated, ClassVar, Literal, TypeGuard, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator
from pydantic.experimental.missing_sentinel import MISSING

type _Check = Callable[[JsonValue, object], bool]


class StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True, frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _check_schema_conditions(cls, data: object) -> object:
        if _is_object(data):
            # MISSING stands for an absent field. Passed explicitly it would satisfy a
            # conditional `required` yet serialize without the field, so it is never input.
            given = [name for name, value in data.items() if value is MISSING]
            if given:
                raise ValueError(f"MISSING given explicitly for {given}; omit the field instead")
            for name, allowed in _literal_fields(cls):
                if name in data and not any(_json_equal(a, data[name]) for a in allowed):
                    raise ValueError(f"{name} must be one of {allowed!r}")
        extra = cls.model_config.get("json_schema_extra")
        if extra is None or callable(extra):
            return data
        conditions = extra.get("allOf")
        if isinstance(conditions, list):
            for condition in conditions:
                if not holds(condition, data):
                    raise ValueError(f"violates schema rule {json.dumps(condition)}")
        return data


@cache
def _literal_fields(model: type[BaseModel]) -> tuple[tuple[str, tuple[JsonValue, ...]], ...]:
    fields = ((name, _literal_values(f.annotation)) for name, f in model.model_fields.items())
    return tuple((name, values) for name, values in fields if values)


def _literal_values(annotation: object) -> tuple[JsonValue, ...]:
    """The allowed values of a Literal field (optionally `| MISSING` or Annotated), else ()."""
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)
    if origin is Annotated:
        return _literal_values(get_args(annotation)[0])
    if origin in (Union, UnionType):
        members = [arg for arg in get_args(annotation) if arg is not MISSING]
        values = [_literal_values(arg) for arg in members]
        if all(values):
            return tuple(v for group in values for v in group)
    return ()


def holds(schema: JsonValue, value: object) -> bool:
    """Evaluates the JSON Schema keyword subset the generator leaves to runtime, plus `type`,
    `items` and `additionalProperties` for pinned output schemas (semantic rule 20).

    An unsupported keyword raises TypeError rather than being skipped: a rule that can't be
    checked must never pass silently.
    """
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        raise TypeError(f"not a schema: {schema!r}")
    if "if" in schema:
        branch = schema.get("then") if holds(schema["if"], value) else schema.get("else")
        if branch is not None and not holds(branch, value):
            return False
    if "additionalProperties" in schema and not _additional(schema, value):
        return False
    for keyword, argument in schema.items():
        if keyword in ("if", "then", "else", "additionalProperties") or keyword in _ANNOTATIONS:
            continue
        check = _CHECKS.get(keyword)
        if check is None:
            raise TypeError(f"unsupported schema keyword {keyword!r}")
        if not check(argument, value):
            return False
    return True


_ANNOTATIONS = frozenset({"description", "title", "default", "examples"})
"""Keywords that describe a value and never constrain it (a Pydantic schema writes them)."""


def _is_object(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, dict)


def _schemas(argument: JsonValue) -> Sequence[JsonValue]:
    if not isinstance(argument, list):
        raise TypeError(f"expected a list of schemas: {argument!r}")
    return argument


def _json_equal(expected: JsonValue, value: object) -> bool:
    # JSON true is not 1 and 1.0 is not the integer 1, though Python's
    # True == 1 == 1.0.
    return type(expected) is type(value) and expected == value


def _required(argument: JsonValue, value: object) -> bool:
    return not _is_object(value) or all(key in value for key in _schemas(argument))


def _properties(argument: JsonValue, value: object) -> bool:
    if not isinstance(argument, dict):
        raise TypeError(f"expected a map of schemas: {argument!r}")
    return not _is_object(value) or all(
        holds(sub, value[key]) for key, sub in argument.items() if key in value
    )


def _additional(schema: dict[str, JsonValue], value: object) -> bool:
    if not _is_object(value):
        return True
    declared = schema.get("properties", {})
    if not isinstance(declared, dict):
        raise TypeError(f"expected a map of schemas: {declared!r}")
    extra = schema["additionalProperties"]
    return all(holds(extra, value[key]) for key in value if key not in declared)


def _is_array(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, list)


def _items(argument: JsonValue, value: object) -> bool:
    return not _is_array(value) or all(holds(argument, item) for item in value)


def _is_integer(value: object) -> bool:
    if isinstance(value, float):
        return value.is_integer()
    return isinstance(value, int) and not isinstance(value, bool)


_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": _is_object,
    "array": _is_array,
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
    "integer": _is_integer,
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
}


def _type(argument: JsonValue, value: object) -> bool:
    names = argument if isinstance(argument, list) else [argument]
    for name in names:
        if not isinstance(name, str) or name not in _JSON_TYPES:
            raise TypeError(f"unknown JSON type {name!r}")
    return any(_JSON_TYPES[str(name)](value) for name in names)


def _min_properties(argument: JsonValue, value: object) -> bool:
    return not _is_object(value) or (isinstance(argument, int) and len(value) >= argument)


_CHECKS: Mapping[str, _Check] = {
    "not": lambda argument, value: not holds(argument, value),
    "allOf": lambda argument, value: all(holds(s, value) for s in _schemas(argument)),
    "anyOf": lambda argument, value: any(holds(s, value) for s in _schemas(argument)),
    "oneOf": lambda argument, value: sum(holds(s, value) for s in _schemas(argument)) == 1,
    "const": _json_equal,
    "enum": lambda argument, value: any(_json_equal(e, value) for e in _schemas(argument)),
    "required": _required,
    "properties": _properties,
    "minProperties": _min_properties,
    "type": _type,
    "items": _items,
}

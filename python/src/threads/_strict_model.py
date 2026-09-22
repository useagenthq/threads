"""Base class of every generated boundary model.

Pydantic types can hold most of the schema, but not cross-field rules (`if`/`then`/`else`,
`not`, `oneOf` over `required`, `minProperties`). The generator copies those rules verbatim from
the schema into each model's `json_schema_extra["allOf"]`, and `StrictModel` checks them against
the raw input before field validation. The rules are data from the schema, never code, so they
can't drift from the TypeScript side.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import ClassVar, TypeGuard

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

type _Check = Callable[[JsonValue, object], bool]


class StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True, frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _check_schema_conditions(cls, data: object) -> object:
        extra = cls.model_config.get("json_schema_extra")
        if extra is None or callable(extra):
            return data
        conditions = extra.get("allOf")
        if isinstance(conditions, list):
            for condition in conditions:
                if not holds(condition, data):
                    raise ValueError(f"violates schema rule {json.dumps(condition)}")
        return data


def holds(schema: JsonValue, value: object) -> bool:
    """Evaluates the JSON Schema keyword subset the generator leaves to runtime."""
    if not isinstance(schema, dict):
        raise TypeError(f"not a schema: {schema!r}")
    if "if" in schema:
        branch = schema.get("then") if holds(schema["if"], value) else schema.get("else")
        if branch is not None and not holds(branch, value):
            return False
    for keyword, argument in schema.items():
        if keyword in ("if", "then", "else", "description"):
            continue
        check = _CHECKS.get(keyword)
        if check is None:
            raise TypeError(f"unsupported schema keyword {keyword!r}")
        if not check(argument, value):
            return False
    return True


def _is_object(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, dict)


def _schemas(argument: JsonValue) -> Sequence[JsonValue]:
    if not isinstance(argument, list):
        raise TypeError(f"expected a list of schemas: {argument!r}")
    return argument


def _json_equal(expected: JsonValue, value: object) -> bool:
    # JSON true is not 1, although Python's True == 1.
    return isinstance(expected, bool) == isinstance(value, bool) and expected == value


def _required(argument: JsonValue, value: object) -> bool:
    return not _is_object(value) or all(key in value for key in _schemas(argument))


def _properties(argument: JsonValue, value: object) -> bool:
    if not isinstance(argument, dict):
        raise TypeError(f"expected a map of schemas: {argument!r}")
    return not _is_object(value) or all(
        holds(sub, value[key]) for key, sub in argument.items() if key in value
    )


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
}

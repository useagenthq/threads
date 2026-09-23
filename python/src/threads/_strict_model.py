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
from functools import cache
from types import UnionType
from typing import Annotated, ClassVar, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator
from pydantic.experimental.missing_sentinel import MISSING

from threads._json_schema import holds, is_object, json_equal


class StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True, frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _check_schema_conditions(cls, data: object) -> object:
        if is_object(data):
            # MISSING stands for an absent field. Passed explicitly it would satisfy a
            # conditional `required` yet serialize without the field, so it is never input.
            given = [name for name, value in data.items() if value is MISSING]
            if given:
                raise ValueError(f"MISSING given explicitly for {given}; omit the field instead")
            for name, allowed in _literal_fields(cls):
                if name in data and not any(json_equal(a, data[name]) for a in allowed):
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

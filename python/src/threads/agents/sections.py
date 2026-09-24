"""agent()'s permissions, retry and context: a complete section, or only the fields to change,
merged over the defaults (spec/api.json agent: a partial of each Policy section)."""

import json
from collections.abc import Mapping

from pydantic import BaseModel, JsonValue, ValidationError

from threads.agents.config import ConfigError

type Section[M: BaseModel] = M | Mapping[str, JsonValue]
"""A complete policy section, or the fields of one to change."""


def completed[M: BaseModel](name: str, given: Section[M] | None, defaults: M) -> M | None:
    """The section to pin: `given` itself when complete, else its fields over `defaults`.
    Raises ConfigError invalid_config for a field the section doesn't have or a wrong value."""
    if given is None or isinstance(given, BaseModel):
        return given
    merged = {**defaults.model_dump(mode="json"), **given}
    try:
        return type(defaults).model_validate_json(json.dumps(merged))
    except ValidationError as error:
        raise ConfigError(
            "invalid_config", f"{name}: {error.error_count()} invalid field(s)"
        ) from error

"""`secret()` (spec/api.json, ): a host secret by name.

A `Secret` holds only its name, so whatever serializes, logs or renders one can't leak a value.
The value is read from the host process environment by `resolve`, called by host code (a host
tool, an MCP client) at the moment it is used. Nothing hands a `Secret` or its value to a
sandbox: sandbox commands run with an empty environment (invariant 4).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from threads.agents.config import ConfigError


@dataclass(frozen=True, slots=True)
class Secret:
    """spec/api.json `Secret`: an opaque reference."""

    name: str

    def __repr__(self) -> str:
        return f"secret({self.name!r})"


def secret(name: str) -> Secret:
    """spec/api.json `secret`. Pure: nothing is read until `resolve`."""
    if not name:
        raise ConfigError("invalid_config", "a secret needs a name")
    return Secret(name)


def resolve(ref: Secret, env: Mapping[str, str] | None = None) -> str:
    """The secret's value, in the host process only. An unset secret is a setup error."""
    value = (os.environ if env is None else env).get(ref.name)
    if not value:
        raise ConfigError("missing_secret", f"secret {ref.name} is not set on the host")
    return value

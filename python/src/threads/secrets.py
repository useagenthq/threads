"""`secret()` (spec/api.json): a host secret by name.

A `Secret` holds only its name, so whatever serializes, logs or renders one can't leak a value.
The value is read from the host process environment by `resolve`, called by host code (a host
tool, an MCP client) at the moment it is used. Nothing hands a `Secret` or its value to a
sandbox: sandbox commands run with an empty environment (invariant 4).

Every value the host resolves, from a `Secret` or an adapter's explicit credential, is
registered here, and `redact_secrets` replaces it in tool-result text before it is recorded
(C5, spec/schema/README.md "Secret redaction").
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from threads.agents.config import ConfigError

_REGISTERED: dict[str, str] = {}
"""Resolved values, each with the smallest label it was registered under."""


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


def _register(value: str, label: str) -> None:
    known = _REGISTERED.get(value)
    if known is None or label < known:
        _REGISTERED[value] = label


def resolve(ref: Secret, env: Mapping[str, str] | None = None) -> str:
    """The secret's value, in the host process only. An unset secret is a setup error."""
    value = (os.environ if env is None else env).get(ref.name)
    if not value:
        raise ConfigError("missing_secret", f"secret {ref.name} is not set on the host")
    _register(value, ref.name)
    return value


def credential(factory: str, option: str, value: str | Secret | None, env: str) -> str:
    """A single-account adapter's credential, resolved on the host at setup: an explicit string
    as given, a `Secret` (default `secret(env)`) from the host environment. Either way the value
    is registered for redaction as `<factory>.<option>`. Missing or empty is missing_secret
    naming both."""
    given = secret(env) if value is None else value
    resolved = os.environ.get(given.name, "") if isinstance(given, Secret) else given
    if not resolved:
        name = given.name if isinstance(given, Secret) else env
        raise ConfigError("missing_secret", f"{factory}: set {option} or {name}")
    _register(resolved, f"{factory}.{option}")
    return resolved


def redact_secrets(text: str) -> str:
    """Replaces every resolved value in `text` with `[secret <label>]`: longest first, so a value
    never leaks the tail of a longer one, then by code point."""
    for value, label in sorted(_REGISTERED.items(), key=lambda item: (-len(item[0]), item[0])):
        text = text.replace(value, f"[secret {label}]")
    return text

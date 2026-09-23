"""`secret()` (spec/api.json): a host secret by name.

A `Secret` holds only its name, so whatever serializes, logs or renders one can't leak a value.
The value is read from the host process environment by `resolve`, called by host code (a host
tool, an MCP client) at the moment it is used. Nothing hands a `Secret` or its value to a
sandbox: sandbox commands run with an empty environment (invariant 4).

Every value the host resolves, from a `Secret` or an adapter's explicit credential, is
registered for redaction (`threads.redaction`): nothing is recorded with one in it (C5).
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from threads.agents.config import ConfigError
from threads.redaction import register


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
    register(value, ref.name)
    return value


def credential(
    factory: str, option: str, value: str | Secret | None, env: str
) -> Callable[[], str]:
    """A single-account adapter's credential. Pure: nothing is read until the getter is called,
    first by the adapter's setup. The first successful call resolves it on the host (an explicit
    string as given, a `Secret`, default `secret(env)`, from the host environment), registers
    it for redaction as `<factory>.<option>` and keeps it, so a client made later in the run
    uses what setup resolved. A failed call keeps nothing and is retried by the next one.
    Missing or empty is missing_secret naming the option and the variable."""
    kept: str | None = None

    def resolved() -> str:
        nonlocal kept
        if kept is None:
            kept = _resolve(factory, option, value, env)
        return kept

    return resolved


def _resolve(factory: str, option: str, value: str | Secret | None, env: str) -> str:
    given = secret(env) if value is None else value
    resolved = os.environ.get(given.name, "") if isinstance(given, Secret) else given
    if not resolved:
        name = given.name if isinstance(given, Secret) else env
        raise ConfigError("missing_secret", f"{factory}: set {option} or {name}")
    register(resolved, f"{factory}.{option}")
    return resolved

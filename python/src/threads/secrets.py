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
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import JsonValue

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
    _register(resolved, f"{factory}.{option}")
    return resolved


def _ordered() -> list[tuple[str, str]]:
    """Longest first, so a value never leaks the tail of a longer one, then by code point."""
    return sorted(_REGISTERED.items(), key=lambda item: (-len(item[0]), item[0]))


def redact_secrets(text: str) -> str:
    """Replaces every resolved value in `text` with `[secret <label>]`."""
    for value, label in _ordered():
        text = text.replace(value, f"[secret {label}]")
    return text


def redact_json(value: JsonValue) -> JsonValue:
    """`value` with every string value in it redacted."""
    match value:
        case str():
            return redact_secrets(value)
        case list():
            return [redact_json(v) for v in value]
        case dict():
            return {k: redact_json(v) for k, v in value.items()}
        case _:
            return value


def _redact_bytes(data: bytes) -> bytes:
    for value, label in _ordered():
        data = data.replace(value.encode(), f"[secret {label}]".encode())
    return data


class StreamRedactor:
    """Redacts bytes as they stream into a recorded artifact (a spilled exec output). It holds
    back the longest value's length less one byte, so a value split across chunks is still
    replaced whole; `end` releases the rest."""

    def __init__(self) -> None:
        self._held = b""

    def feed(self, chunk: bytes) -> bytes:
        data = _redact_bytes(self._held + chunk)
        longest = max((len(v.encode()) for v in _REGISTERED), default=0)
        keep = min(len(data), max(0, longest - 1))
        self._held = data[len(data) - keep :]
        return data[: len(data) - keep]

    def end(self) -> bytes:
        rest, self._held = _redact_bytes(self._held), b""
        return rest

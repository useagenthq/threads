"""The registered values: every credential the host resolves (spec/schema/README.md "Secret
redaction", C5). A value is at least 8 characters and never a literal of the event schema, so
the line envelope and the schema's keys can stay unredacted: none can hold one."""

from functools import cache

from pydantic import JsonValue, TypeAdapter

from threads._generated.events_v1 import ErrorCode
from threads.agents.config import ConfigError
from threads.log.parse import LogLine

MIN_CHARS = 8

REGISTERED: dict[str, str] = {}
"""Values resolved in this host process, each with the smallest label it was registered under."""


@cache
def _schema_literals() -> frozenset[str]:
    """Every event type, enum value and field name of the event schema, read from the
    generated models."""
    schemas: list[JsonValue] = [
        TypeAdapter[object](LogLine).json_schema(),
        TypeAdapter[object](ErrorCode).json_schema(),
    ]
    return frozenset(s for node in schemas for s in _collect(node))


def _collect(node: JsonValue) -> list[str]:
    match node:
        case list():
            return [s for item in node for s in _collect(item)]
        case dict():
            found: list[str] = []
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    found += list(value)
                if key == "enum" and isinstance(value, list):
                    found += [v for v in value if isinstance(v, str)]
                if key == "const" and isinstance(value, str):
                    found.append(value)
                found += _collect(value)
            return found
        case _:
            return []


def register(value: str, label: str) -> None:
    """Registers a resolved value; recorded text shows `[secret <label>]` instead. A value
    shorter than 8 characters, or equal to a schema literal, is refused (invalid_config)."""
    if len(value) < MIN_CHARS:
        raise ConfigError("invalid_config", "secret values must be at least 8 characters")
    if value in _schema_literals():
        raise ConfigError("invalid_config", "a secret value can't be a literal of the event schema")
    known = REGISTERED.get(value)
    if known is None or label < known:
        REGISTERED[value] = label


def forget_secrets() -> None:
    """Forgets every registered value: each test starts with none (tests/conftest.py)."""
    REGISTERED.clear()


def ordered() -> list[tuple[str, str]]:
    """Value and label, longest value first, then by code point."""
    return sorted(REGISTERED.items(), key=lambda item: (-len(item[0]), item[0]))


def holds(text: str) -> bool:
    """Whether `text` holds a registered value."""
    return any(v in text for v in REGISTERED)

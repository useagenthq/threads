"""Configuration: an option, then the traces variable, then the generic one, then the default
(spec/otel/README.md, "Configuration"; pinned by spec/otel/vectors/otel-env.json)."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import unquote_to_bytes

from threads.agents.config import ConfigError
from threads.otel.attrs import SEMCONV

_T: Final = "OTEL_EXPORTER_OTLP_TRACES_"
_G: Final = "OTEL_EXPORTER_OTLP_"
_UNSUPPORTED: Final = ("CERTIFICATE", "CLIENT_CERTIFICATE", "CLIENT_KEY")
_BAD_ESCAPE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")


@dataclass(frozen=True, slots=True)
class Options:
    endpoint: str | None = None
    headers: Mapping[str, str] | None = None
    service: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    endpoint: str
    headers: Mapping[str, str]
    timeout_ms: int
    compression: Literal["none", "gzip"]
    resource: Mapping[str, str]


def _refused(message: str) -> Exception:
    """A refused setting: its message names the option or variable, never a header value."""
    return ConfigError("invalid_config", f"otel(): {message}")


def _pick(env: Mapping[str, str], name: str) -> tuple[str, str] | None:
    """The first set variable (name, value) of the traces and the generic form of `name`."""
    for variable in (f"{_T}{name}", f"{_G}{name}"):
        value = env.get(variable, "")
        if value:
            return variable, value
    return None


def _decode(text: str, variable: str) -> str:
    """Strict percent-decoding, as JavaScript's decodeURIComponent: a malformed escape or bytes
    that aren't UTF-8 are refused."""
    try:
        if _BAD_ESCAPE.search(text):
            raise ValueError(text)
        return unquote_to_bytes(text).decode("utf-8")
    except ValueError:
        raise _refused(f"{variable} has an invalid percent escape") from None


def parse_list(text: str, variable: str) -> dict[str, str]:
    """`k=v,k=v`, split on "," then the first "=", trimmed and percent-decoded."""
    out: dict[str, str] = {}
    for entry in text.split(","):
        if not entry.strip():
            continue
        key, eq, value = entry.partition("=")
        name = _decode(key.strip(), variable) if eq else ""
        if not name:
            raise _refused(f"{variable}: every entry must be key=value with a non-empty key")
        out[name] = _decode(value.strip(), variable)
    return out


def _endpoint(options: Options, env: Mapping[str, str]) -> str:
    if options.endpoint is not None:
        return options.endpoint
    if env.get(f"{_T}ENDPOINT"):
        return env[f"{_T}ENDPOINT"]
    generic = env.get(f"{_G}ENDPOINT", "")
    if generic:
        return f"{generic.rstrip('/')}/v1/traces"
    raise _refused(
        f"no collector endpoint: pass otel(endpoint=...) or set {_T}ENDPOINT or {_G}ENDPOINT"
    )


def _timeout(env: Mapping[str, str]) -> int:
    picked = _pick(env, "TIMEOUT")
    if picked is None:
        return 10_000
    name, value = picked
    if not re.fullmatch(r"\d+", value) or int(value) <= 0:
        raise _refused(f"{name} must be a positive number of milliseconds")
    return int(value)


def _compression(env: Mapping[str, str]) -> Literal["none", "gzip"]:
    picked = _pick(env, "COMPRESSION")
    if picked is None or picked[1] == "none":
        return "none"
    if picked[1] == "gzip":
        return "gzip"
    raise _refused(f"{picked[0]}: compression must be none or gzip")


def _resource(options: Options, env: Mapping[str, str], language: str) -> dict[str, str]:
    variable = env.get("OTEL_RESOURCE_ATTRIBUTES", "")
    attrs = parse_list(variable, "OTEL_RESOURCE_ATTRIBUTES") if variable else {}
    service = options.service
    if service is None:
        service = env.get("OTEL_SERVICE_NAME") or attrs.get("service.name") or "threads"
    return {
        **attrs,
        "service.name": service,
        "telemetry.sdk.name": "threads",
        "telemetry.sdk.language": language,
        "threads.otel.semconv": SEMCONV,
    }


def configure(options: Options, env: Mapping[str, str], language: str) -> Config:
    """The exporter's settings; a refused one names what to fix (invalid_config)."""
    for name in _UNSUPPORTED:
        for variable in (f"{_T}{name}", f"{_G}{name}"):
            if env.get(variable):
                raise _refused(
                    f"{variable} is set, but client certificates are not supported: put an"
                    " OpenTelemetry Collector in front"
                )
    url = _endpoint(options, env)
    protocol = _pick(env, "PROTOCOL")
    if protocol is not None and protocol[1] != "http/json":
        raise _refused(f"{protocol[0]}={protocol[1]}: only http/json is supported")
    timeout_ms, compression = _timeout(env), _compression(env)
    headers = _pick(env, "HEADERS")
    return Config(
        url,
        options.headers
        if options.headers is not None
        else (parse_list(headers[1], headers[0]) if headers else {}),
        timeout_ms,
        compression,
        _resource(options, env, language),
    )

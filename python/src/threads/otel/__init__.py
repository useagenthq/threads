"""`from threads.otel import otel` (extra `otel`): traces derived from the log, sent to an
OpenTelemetry collector over OTLP/HTTP JSON (spec/otel/README.md). No OpenTelemetry SDK: span ids
come from event ids, so a re-send carries the same ids, and the bytes match the TypeScript
exporter's."""

import os
from collections.abc import Mapping

from threads.agents.store import Store
from threads.otel.env import Options, configure
from threads.otel.exporter import OtelExporter
from threads.telemetry import Exporter


def otel(  # noqa: PLR0913 - spec/api.json otel's options
    *,
    store: Store | None = None,
    endpoint: str | None = None,
    headers: Mapping[str, str] | None = None,
    service: str | None = None,
    name: str | None = None,
    content: bool = False,
) -> Exporter:
    """spec/api.json `otel`: an Exporter for `host(telemetry=otel())`, or to call `sync()` on
    yourself. Options not given come from the standard OTEL_EXPORTER_OTLP_* variables; a setting
    that can't work is an invalid_config setup error naming the option or variable. `store`
    omitted: the store of the host it is passed to. `name` names the exporter's cursor (observer
    otel:<name>). `content`: tool arguments and results and response text, as logged (already
    redacted); prompts are never exported."""
    config = configure(Options(endpoint, headers, service), os.environ, "python")
    observer = "otel" if name is None else f"otel:{name}"
    return OtelExporter(config, store, observer, content=content)


__all__ = ["otel"]

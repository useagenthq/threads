# pyright: strict
"""spec/otel/vectors: the id derivation (otel-ids.json) and the configuration resolution
(otel-env.json) of spec/otel/README.md."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import BRANCH, CHILD, THREAD, eid
from .jcs import JsonValue, Obj
from .otel_expect import SEMCONV, loss_ids, nonzero, span_id, trace_id
from .pieces import dump

if TYPE_CHECKING:
    import pathlib

T = "OTEL_EXPORTER_OTLP_TRACES_"
G = "OTEL_EXPORTER_OTLP_"
URL = "https://collector.example:4318"


def build(root: pathlib.Path) -> None:
    root.mkdir(parents=True)
    (root / "otel-ids.json").write_text(dump(_ids()))
    (root / "otel-env.json").write_text(dump({"cases": _env()}))


def _ids() -> Obj:
    e1, e2 = eid(1), eid(2, CHILD)
    spans: list[JsonValue] = [
        {"branch_id": BRANCH, "event_id": e1, "span_id": span_id(BRANCH, e1)},
        {"branch_id": CHILD, "event_id": e2, "span_id": span_id(CHILD, e2)},
        {
            "branch_id": BRANCH,
            "event_id": e1,
            "call_id": "call_1",
            "span_id": span_id(BRANCH, e1, "call_1"),
        },
    ]
    traces: list[JsonValue] = [
        {"thread_id": THREAD, "event_id": e1, "trace_id": trace_id(THREAD, e1)},
        {"thread_id": "té", "event_id": "e", "trace_id": trace_id("té", "e")},
    ]
    loss_trace, loss_span = loss_ids("otel", THREAD, 1790000000000)
    losses: list[JsonValue] = [
        {
            "observer": "otel",
            "thread_id": THREAD,
            "deleted_at": 1790000000000,
            "trace_id": loss_trace,
            "span_id": loss_span,
        }
    ]
    zero: list[JsonValue] = [
        {"digest": "0000000000000000", "id": nonzero(bytes(8))},
        {"digest": "00" * 16, "id": nonzero(bytes(16))},
        {"digest": "0000000000000100", "id": nonzero(bytes.fromhex("0000000000000100"))},
    ]
    return {"spans": spans, "traces": traces, "losses": losses, "nonzero": zero}


def _ok(endpoint: str, **more: JsonValue) -> Obj:
    resource: Obj = {
        "service.name": "threads",
        "telemetry.sdk.name": "threads",
        "threads.otel.semconv": SEMCONV,
    }
    extra = more.pop("resource", {})
    if isinstance(extra, dict):
        resource |= extra
    out: Obj = {
        "endpoint": endpoint,
        "headers": {},
        "timeout_ms": 10000,
        "compression": "none",
        "resource": resource,
    }
    return out | more


def _case(name: str, env: Obj, expected: Obj, options: Obj | None = None) -> Obj:
    return {"name": name, "env": env, "options": options or {}, "expected": expected}


def _refused(name: str, env: Obj, *variables: str) -> Obj:
    return _case(name, env, {"error": "invalid_config", "names": list[JsonValue](variables)})


def _env() -> list[JsonValue]:
    base: Obj = {f"{G}ENDPOINT": URL}
    traces = f"{URL}/custom/traces"
    return [
        _case("generic endpoint gets /v1/traces", base, _ok(f"{URL}/v1/traces")),
        _case("one slash before /v1/traces", {f"{G}ENDPOINT": f"{URL}/"}, _ok(f"{URL}/v1/traces")),
        _case("traces endpoint is used as is", {f"{T}ENDPOINT": traces}, _ok(traces)),
        _case("traces beats generic", {**base, f"{T}ENDPOINT": traces}, _ok(traces)),
        _case(
            "the option beats both",
            {**base, f"{T}ENDPOINT": traces},
            _ok("http://localhost:4318/v1/traces"),
            {"endpoint": "http://localhost:4318/v1/traces"},
        ),
        _refused("no endpoint names both variables", {}, f"{T}ENDPOINT", f"{G}ENDPOINT"),
        _refused(
            "an empty variable is unset", {f"{G}ENDPOINT": ""}, f"{T}ENDPOINT", f"{G}ENDPOINT"
        ),
        _case(
            "headers are split and percent-decoded",
            {**base, f"{G}HEADERS": "x-honeycomb-team=abc%20def , x-b=1"},
            _ok(f"{URL}/v1/traces", headers={"x-honeycomb-team": "abc def", "x-b": "1"}),
        ),
        _case(
            "traces headers replace generic ones",
            {**base, f"{G}HEADERS": "a=1", f"{T}HEADERS": "b=2"},
            _ok(f"{URL}/v1/traces", headers={"b": "2"}),
        ),
        _case(
            "the headers option beats both",
            {**base, f"{T}HEADERS": "b=2"},
            _ok(f"{URL}/v1/traces", headers={"c": "3"}),
            {"headers": {"c": "3"}},
        ),
        _refused(
            "a header without = is refused", {**base, f"{G}HEADERS": "s3cret-token"}, f"{G}HEADERS"
        ),
        _refused(
            "an empty header key is refused",
            {**base, f"{T}HEADERS": "=s3cret-token"},
            f"{T}HEADERS",
        ),
        _refused(
            "a bad percent escape is refused", {**base, f"{G}HEADERS": "a=%zz"}, f"{G}HEADERS"
        ),
        _refused(
            "a percent escape that is not UTF-8 is refused",
            {**base, "OTEL_RESOURCE_ATTRIBUTES": "a=%C3"},
            "OTEL_RESOURCE_ATTRIBUTES",
        ),
        _case(
            "empty entries are skipped",
            {**base, f"{G}HEADERS": "a=1,, ,b="},
            _ok(f"{URL}/v1/traces", headers={"a": "1", "b": ""}),
        ),
        _case(
            "http/json is accepted", {**base, f"{G}PROTOCOL": "http/json"}, _ok(f"{URL}/v1/traces")
        ),
        _refused("grpc is refused", {**base, f"{G}PROTOCOL": "grpc"}, f"{G}PROTOCOL"),
        _refused(
            "traces protobuf is refused",
            {**base, f"{G}PROTOCOL": "http/json", f"{T}PROTOCOL": "http/protobuf"},
            f"{T}PROTOCOL",
        ),
        _case(
            "traces timeout beats generic",
            {**base, f"{G}TIMEOUT": "5000", f"{T}TIMEOUT": "2500"},
            _ok(f"{URL}/v1/traces", timeout_ms=2500),
        ),
        _refused("a bad timeout is refused", {**base, f"{G}TIMEOUT": "10s"}, f"{G}TIMEOUT"),
        _refused("a zero timeout is refused", {**base, f"{T}TIMEOUT": "0"}, f"{T}TIMEOUT"),
        _case(
            "gzip", {**base, f"{T}COMPRESSION": "gzip"}, _ok(f"{URL}/v1/traces", compression="gzip")
        ),
        _refused("unknown compression", {**base, f"{G}COMPRESSION": "br"}, f"{G}COMPRESSION"),
        _refused("a client key is refused", {**base, f"{G}CLIENT_KEY": "/k.pem"}, f"{G}CLIENT_KEY"),
        _refused(
            "a certificate is refused", {**base, f"{T}CERTIFICATE": "/c.pem"}, f"{T}CERTIFICATE"
        ),
        _case(
            "OTEL_SERVICE_NAME names the service",
            {**base, "OTEL_SERVICE_NAME": "support-bot"},
            _ok(f"{URL}/v1/traces", resource={"service.name": "support-bot"}),
        ),
        _case(
            "the service option beats OTEL_SERVICE_NAME",
            {**base, "OTEL_SERVICE_NAME": "support-bot"},
            _ok(f"{URL}/v1/traces", resource={"service.name": "billing"}),
            {"service": "billing"},
        ),
        _case(
            "resource attributes are merged, and never override threads' own",
            {
                **base,
                "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=prod,team=a%2Cb,"
                "telemetry.sdk.name=other,service.name=from-resource",
            },
            _ok(
                f"{URL}/v1/traces",
                resource={
                    "deployment.environment": "prod",
                    "team": "a,b",
                    "service.name": "from-resource",
                },
            ),
        ),
        _case(
            "OTEL_SERVICE_NAME beats service.name in the resource",
            {**base, "OTEL_RESOURCE_ATTRIBUTES": "service.name=x", "OTEL_SERVICE_NAME": "y"},
            _ok(f"{URL}/v1/traces", resource={"service.name": "y"}),
        ),
        _refused(
            "a resource entry without = is refused",
            {**base, "OTEL_RESOURCE_ATTRIBUTES": "prod"},
            "OTEL_RESOURCE_ATTRIBUTES",
        ),
    ]

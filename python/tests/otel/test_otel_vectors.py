"""spec/otel/vectors: the id derivation and the configuration precedence, as TypeScript reads
them (typescript/packages/otel/test/vectors.test.ts)."""

import pytest
from otel_goldens_kit import read_json
from pydantic import BaseModel, ConfigDict

from threads.agents.config import ConfigError
from threads.otel.env import Options, configure
from threads.otel.ids import loss_ids, nonzero, span_id, trace_id


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpanVector(_Strict):
    branch_id: str
    event_id: str
    call_id: str | None = None
    span_id: str


class TraceVector(_Strict):
    thread_id: str
    event_id: str
    trace_id: str


class LossVector(_Strict):
    observer: str
    thread_id: str
    deleted_at: int
    trace_id: str
    span_id: str


class ZeroVector(_Strict):
    digest: str
    id: str


class Ids(_Strict):
    spans: list[SpanVector]
    traces: list[TraceVector]
    losses: list[LossVector]
    nonzero: list[ZeroVector]


class Refused(_Strict):
    error: str
    names: list[str]


class Resolved(_Strict):
    endpoint: str
    headers: dict[str, str]
    timeout_ms: int
    compression: str
    resource: dict[str, str]


class EnvOptions(_Strict):
    endpoint: str | None = None
    headers: dict[str, str] | None = None
    service: str | None = None


class EnvCase(_Strict):
    name: str
    env: dict[str, str]
    options: EnvOptions
    expected: Refused | Resolved


class EnvFile(_Strict):
    cases: list[EnvCase]


IDS = Ids.model_validate(read_json("otel-ids.json"))
ENV = EnvFile.model_validate(read_json("otel-env.json")).cases


def test_span_trace_and_loss_ids() -> None:
    for v in IDS.spans:
        assert span_id(v.branch_id, v.event_id, v.call_id) == v.span_id
    for t in IDS.traces:
        assert trace_id(t.thread_id, t.event_id) == t.trace_id
    for loss in IDS.losses:
        assert loss_ids(loss.observer, loss.thread_id, loss.deleted_at) == (
            loss.trace_id,
            loss.span_id,
        )


def test_an_all_zero_id_gets_its_last_byte_set_to_one() -> None:
    for z in IDS.nonzero:
        assert nonzero(z.digest) == z.id


@pytest.mark.parametrize("case", ENV, ids=[c.name for c in ENV])
def test_configuration_vectors(case: EnvCase) -> None:
    o = case.options
    options = Options(o.endpoint, o.headers, o.service)
    expected = case.expected
    if isinstance(expected, Refused):
        with pytest.raises(ConfigError) as refused:
            configure(options, case.env, "python")
        assert refused.value.code == expected.error
        message = str(refused.value)
        assert all(name in message for name in expected.names)
        # Header values are credentials: never in a message.
        assert not [v for k, v in case.env.items() if k.endswith("HEADERS") and v in message]
        return
    got = configure(options, case.env, "python")
    resource = dict(got.resource)
    assert resource.pop("telemetry.sdk.language") == "python"
    assert (got.endpoint, dict(got.headers), got.timeout_ms, got.compression, resource) == (
        expected.endpoint,
        expected.headers,
        expected.timeout_ms,
        expected.compression,
        expected.resource,
    )

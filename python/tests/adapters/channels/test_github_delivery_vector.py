"""The shared GitHub delivery vector: identity comes only from the signed body, so an unsigned
X-GitHub-Delivery (changed or missing) never changes the delivery id, and a body the signature
doesn't cover is unverified."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from threads.github import github
from threads.host import RawRequest
from threads.result import Err, Ok
from threads.secrets import secret

VECTOR_PATH = Path(__file__).resolve().parents[4] / "spec/conformance/vectors/github-delivery.json"


def _vector() -> dict[str, JsonValue]:
    loaded: JsonValue = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


VECTOR = _vector()


def _text(key: str) -> str:
    value = VECTOR[key]
    assert isinstance(value, str)
    return value


def _headers() -> list[str | None]:
    values = VECTOR["accepted_delivery_headers"]
    assert isinstance(values, list)
    return [v if isinstance(v, str) else None for v in values]


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREADS_GH_VECTOR_WEBHOOK", _text("webhook_secret"))
    monkeypatch.setenv("THREADS_GH_VECTOR_TOKEN", "tok")


def _raw(body: str, delivery: str | None) -> RawRequest:
    headers = {"x-hub-signature-256": _text("signature"), "x-github-event": _text("event")}
    if delivery is not None:
        headers["x-github-delivery"] = delivery
    return RawRequest(headers, body.encode())


@pytest.mark.parametrize("header", _headers())
def test_the_delivery_id_is_the_body_hash_whatever_the_header(header: str | None) -> None:
    channel = github(
        webhook_secret=secret("THREADS_GH_VECTOR_WEBHOOK"),
        token=secret("THREADS_GH_VECTOR_TOKEN"),
        agent="triage",
    )
    match channel.verify(_raw(_text("body"), header)):
        case Ok(value=verified):
            assert verified.tenant == _text("tenant")
            assert verified.installation_id == _text("installation_id")
            assert verified.delivery_id == _text("delivery_id")
        case Err(error=error):
            pytest.fail(error.message)


def test_a_body_the_signature_does_not_cover_is_unverified() -> None:
    channel = github(
        webhook_secret=secret("THREADS_GH_VECTOR_WEBHOOK"),
        token=secret("THREADS_GH_VECTOR_TOKEN"),
        agent="triage",
    )
    assert isinstance(channel.verify(_raw(_text("tampered_body"), "d-1")), Err)

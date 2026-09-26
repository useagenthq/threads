"""The sandbox factories built without their defaulted options (spec/api.json functions e2b,
daytona and modal): what a create then sends, and what the sandbox declares."""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from aiohttp.test_utils import TestServer
from daytona_server import API_KEY as DAYTONA_KEY
from daytona_server import DaytonaServer
from docker_fake import DockerEngine, TracedTransport
from e2b_fake import API_KEY as E2B_KEY
from e2b_fake import control, transports
from modal_fake import TOKEN_ID, TOKEN_SECRET, FakeModal, harness
from sandbox_backend import FakeBackend
from sandbox_kit import OPEN

from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes.docker import transport as docker_transport
from threads.adapters.sandboxes.docker.sandbox import IMAGE
from threads.adapters.sandboxes.e2b import sandbox as e2b_module
from threads.agents.config import ConfigError
from threads.daytona import daytona
from threads.docker import docker
from threads.e2b import e2b
from threads.modal import modal
from threads.result import Ok

HOUR_MS = 3_600_000


@pytest.mark.parametrize("lifetime_ms", [0, -60_000])
def test_a_lifetime_that_is_not_positive_is_invalid_config(lifetime_ms: int) -> None:
    """Daytona reads a TTL of 0 as no TTL: a sandbox declared expired would live on."""
    for make in (
        lambda: e2b(api_key=E2B_KEY, lifetime_ms=lifetime_ms),
        lambda: daytona(api_key=DAYTONA_KEY, lifetime_ms=lifetime_ms),
    ):
        with pytest.raises(ConfigError) as refused:
            make()
        assert refused.value.code == "invalid_config"


def test_e2b_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """template "base", a one-hour lifetime, no internet, provider name "e2b"."""
    backend = FakeBackend.scripted()
    bodies: list[dict[str, object]] = []
    answer = control(backend)

    def spy(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/sandboxes":
            bodies.append(json.loads(request.content))
        return answer(request)

    pair = transports(backend, spy)
    monkeypatch.setattr(e2b_module, "http_transport", lambda: pair.http)
    monkeypatch.setattr(e2b_module, "rpc_transport", lambda: pair.rpc)
    made = e2b(api_key=E2B_KEY)

    async def create() -> None:
        async with holding():
            assert isinstance(await made.create("k", OPEN), Ok)

    asyncio.run(create())
    (body,) = bodies
    assert (body["templateID"], body["timeout"], body["allow_internet_access"]) == (
        "base",
        HOUR_MS // 1000,
        False,
    )
    assert (made.lifetime_ms, made.info.egress, made.info.provider) == (
        HOUR_MS,
        "enforced",
        "e2b",
    )


def test_daytona_defaults() -> None:
    """A one-hour lifetime, the network blocked, a 60-minute auto-stop, Daytona's default
    snapshot and region, provider name "daytona"."""

    async def create() -> DaytonaServer:
        server = DaytonaServer(FakeBackend.scripted())
        async with TestServer(server.app, host="127.0.0.1") as test:
            server.base = str(test.make_url("")).rstrip("/")
            made = daytona(api_key=DAYTONA_KEY, api_url=server.base)
            assert (made.lifetime_ms, made.info.egress, made.info.provider) == (
                HOUR_MS,
                "enforced",
                "daytona",
            )
            async with holding():
                assert isinstance(await made.create("k", OPEN), Ok)
        return server

    (sent,) = asyncio.run(create()).created
    assert (sent.ttl_minutes, sent.network_block_all, sent.auto_stop_interval) == (60, True, 60)
    assert (sent.snapshot, sent.target) == (None, None)


def test_modal_defaults() -> None:
    """App "threads" in the default environment, a one-hour lifetime, no internet, provider
    name "modal"."""
    fake = FakeModal(FakeBackend.scripted())

    async def create() -> None:
        async with harness(fake.backend, "modal", fake) as sandbox:
            assert (sandbox.lifetime_ms, sandbox.info.egress) == (HOUR_MS, "enforced")
            assert isinstance(await sandbox.create("k", OPEN), Ok)

    asyncio.run(create())
    (app,) = fake.apps
    assert (app.app_name, app.environment_name) == ("threads", "")
    (made,) = fake.creates
    assert (made.definition.timeout_secs, made.definition.block_network) == (HOUR_MS // 1000, True)
    named = modal(image_id="im-x", token_id=TOKEN_ID, token_secret=TOKEN_SECRET)
    assert named.info.provider == "modal"


def test_docker_defaults() -> None:
    """The pinned node:22-bookworm image, no internet, provider name "docker", and a container
    with no network, a fixed PidsLimit and no limit it wasn't given."""
    backend = FakeBackend.scripted()
    engine = DockerEngine(backend)
    made = docker(transport=lambda: TracedTransport(backend, engine.handle))
    info = made.info
    assert (info.provider, info.egress) == ("docker", "enforced")
    assert (info.lookup.create, info.lookup.snapshot) == ("nonfinal", "none")
    # ponytail: Docker's snapshots are core's host trees (lane 16C), which isn't on main.
    assert info.capture_classes == ()
    assert info.termination == "confirmed"

    async def create() -> None:
        async with holding():
            assert isinstance(await made.create("k", OPEN), Ok)

    asyncio.run(create())
    (body,) = engine.created
    assert body["Image"] == IMAGE
    assert IMAGE.startswith("node:22-bookworm@sha256:")
    host = body["HostConfig"]
    assert (host["NetworkMode"], host["PidsLimit"]) == ("none", 1024)
    assert "Memory" not in host
    assert "NanoCpus" not in host


def test_a_docker_limit_that_cannot_be_expressed_is_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every limit Docker can't express, and every socket it can't speak, named with its
    value before anything is created."""
    refusals = [
        lambda: docker(cpus=0),
        lambda: docker(cpus=-1),
        lambda: docker(memory_mb=8),
        lambda: docker(memory_mb=64.5),  # pyright: ignore[reportArgumentType] - an untyped caller
        lambda: docker(name="Not-A-Name"),
    ]
    for make in refusals:
        with pytest.raises(ConfigError) as refused:
            make()
        assert refused.value.code == "invalid_config"
        assert refused.value.message.startswith("docker: ")
    assert "not -1" in _refusal(lambda: docker(cpus=-1))
    assert "not 8" in _refusal(lambda: docker(memory_mb=8))
    monkeypatch.setenv(docker_transport.DOCKER_HOST, "tcp://127.0.0.1:2375")
    with pytest.raises(ConfigError) as refused:
        asyncio.run(docker().setup())
    assert refused.value.code == "invalid_config"
    assert refused.value.message == (
        "docker: DOCKER_HOST must be a unix:// socket, not tcp://127.0.0.1:2375"
    )


def _refusal(make: Callable[[], object]) -> str:
    with pytest.raises(ConfigError) as refused:
        make()
    return refused.value.message

"""Exporter.sync() on a real store against a fake local collector: each span sent once, at
least once across a crash, nothing moved when the collector fails."""

import asyncio
import gzip
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from otel_collector_kit import collector
from otel_store_kit import cursors, looping

from threads import Completed, ConfigError, agent, scripted_model, sqlite
from threads.agents.store import open_store
from threads.otel import otel
from threads.redaction import register
from threads.result import Err, Ok
from threads.telemetry import Exporter, SyncReport

if TYPE_CHECKING:
    from pydantic import JsonValue


async def _synced(exporter: Exporter) -> SyncReport:
    sent = await exporter.sync()
    assert isinstance(sent, Ok), sent
    return sent.value


def test_a_40_model_call_turn_sends_every_span_exactly_once(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            exporter = otel(store=store, endpoint=c.url)

            async def tick() -> None:
                await _synced(exporter)

            result = await looping(40, tick).run("loop", store=store)
            assert isinstance(result, Completed)
            before_end = c.span_ids()
            await _synced(exporter)
            ids = c.span_ids()
            assert len(ids) == len(set(ids)) == 40 + 39 + 1  # chats, tools, the turn
            turn = [
                str(s["spanId"])
                for r in c.accepted()
                for s in r.spans()
                if str(s["name"]).startswith("invoke_agent")
            ]
            assert len(turn) == 1
            assert turn[0] not in before_end  # sent only after turn_completed
            assert await _synced(exporter) == SyncReport(0, 0, ())

    asyncio.run(main())


def test_a_failed_post_moves_no_cursor_and_the_retry_sends_the_same_bytes(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            assert isinstance(await looping(3).run("go", store=store), Completed)
            exporter = otel(store=store, endpoint=c.url)
            c.statuses = [503]
            failed = await exporter.sync()
            assert isinstance(failed, Err)
            assert (failed.error.code, failed.error.status) == ("collector_unavailable", 503)
            assert await cursors(store) == {}
            assert (await _synced(exporter)).spans == 6  # noqa: PLR2004 - counted spans
            assert c.received[0].body == c.received[1].body

    asyncio.run(main())


def test_a_crash_after_the_ack_resends_the_same_span_ids(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            assert isinstance(await looping(2).run("go", store=store), Completed)
            exporter = otel(store=store, endpoint=c.url)
            await _synced(exporter)
            first = c.span_ids()
            # The crash: the collector acknowledged, the cursor write never happened.
            sq = await open_store(store)
            await sq.run(lambda conn: conn.execute("DELETE FROM observer_cursors"))
            await _synced(otel(store=store, endpoint=c.url))
            assert c.span_ids() == first + first

    asyncio.run(main())


def test_refused_and_rejected_collectors(tmp_path: Path) -> None:
    async def main() -> None:
        store = sqlite(str(tmp_path / "s"))
        assert isinstance(await looping(2).run("go", store=store), Completed)
        async with collector() as gone:
            url = gone.url
        refused = await otel(store=store, endpoint=url).sync()
        assert isinstance(refused, Err)
        assert refused.error.code == "collector_unavailable"
        async with collector() as c:
            c.statuses = [400]
            rejected = await otel(store=store, endpoint=c.url).sync()
            assert isinstance(rejected, Err)
            assert (rejected.error.code, rejected.error.status) == ("collector_rejected", 400)
            assert "bad spans" in rejected.error.message
        assert await cursors(store) == {}

    asyncio.run(main())


def test_gzip_sends_the_same_body_compressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            assert isinstance(await looping(2).run("go", store=store), Completed)
            await _synced(otel(store=store, endpoint=c.url, name="plain"))
            monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "gzip")
            await _synced(otel(store=store, endpoint=c.url, name="zipped"))
            plain, zipped = c.received
            assert zipped.headers["content-encoding"] == "gzip"
            assert gzip.decompress(zipped.body) == plain.body

    asyncio.run(main())


def test_content_is_off_by_default_and_a_secret_never_leaves(tmp_path: Path) -> None:
    secret = "sk-live-0123456789abcdef"  # noqa: S105 - a test value

    async def main() -> None:
        async with collector() as c:
            register(secret, "API_KEY")
            store = sqlite(str(tmp_path / "s"))
            ran = await looping(2, said=f"key {secret}").run("go", store=store)
            assert isinstance(ran, Completed)
            await _synced(otel(store=store, endpoint=c.url))
            await _synced(otel(store=store, endpoint=c.url, name="content", content=True))
            off, on = c.received
            assert b"gen_ai.tool.call.arguments" not in off.body
            assert b"threads.model.output_text" not in off.body
            assert b"gen_ai.tool.call.arguments" in on.body
            assert secret.encode() not in off.body + on.body

    asyncio.run(main())


def test_a_secret_split_across_text_parts_is_never_joined(tmp_path: Path) -> None:
    secret = "sk-live-split-0123456789"  # noqa: S105 - a test value
    halves = [secret[:8], secret[8:]]

    async def main() -> None:
        async with collector() as c:
            register(secret, "API_KEY")
            store = sqlite(str(tmp_path / "s"))
            reply: JsonValue = {
                "content": [{"type": "text", "text": h} for h in halves],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            bot = agent(model=scripted_model({"responses": [reply]}))
            assert isinstance(await bot.run("go", store=store), Completed)
            await _synced(otel(store=store, endpoint=c.url, content=True))
            body = b"".join(r.body for r in c.received)
            values = ",".join(f'{{"stringValue":"{h}"}}' for h in halves)
            assert f'{{"arrayValue":{{"values":[{values}]}}}}'.encode() in body
            assert secret.encode() not in body

    asyncio.run(main())


def test_errors_name_the_endpoint_without_its_credentials(tmp_path: Path) -> None:
    async def main() -> None:
        store = sqlite(str(tmp_path / "s"))
        assert isinstance(await looping(1).run("go", store=store), Completed)
        url = "http://user:pa55word@127.0.0.1:9/v1/traces?api_key=SEKRET123"
        refused = await otel(store=store, endpoint=url).sync()
        assert isinstance(refused, Err)
        assert "http://127.0.0.1:9/v1/traces is unavailable" in refused.error.message
        assert "SEKRET123" not in refused.error.message
        assert "pa55word" not in refused.error.message

    asyncio.run(main())


def test_no_store_outside_a_host_is_a_setup_error() -> None:
    async def main() -> None:
        with pytest.raises(ConfigError) as refused:
            await otel(endpoint="http://127.0.0.1:9/v1/traces").sync()
        assert refused.value.code == "invalid_config"

    asyncio.run(main())


def test_a_missing_endpoint_is_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as refused:
        otel()
    assert refused.value.code == "invalid_config"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in str(refused.value)

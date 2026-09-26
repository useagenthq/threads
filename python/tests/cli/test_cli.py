"""The `threads` CLI: thin wrappers over the store and the host (7 and 8). Export is the stored
bytes, import verifies and stores the same bytes, delete
removes a thread whole, gc sweeps only unreferenced artifacts past the grace period, and dev
prints each channel's webhook URL before serving."""

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from pydantic import JsonValue

from threads import Completed, agent, scripted_model, sqlite
from threads.cli import main, serve
from threads.host import Host

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
REPLY: JsonValue = {
    "content": [{"type": "text", "text": "Hi."}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}
OLD = 1_000_000_000.0
"""An mtime far past the gc grace period."""


def ran(home: Path) -> Completed[str]:
    async def run() -> Completed[str]:
        bot = agent(model=scripted_model({"responses": [REPLY]}))
        result = await bot.run("hi", store=sqlite(str(home)))
        assert isinstance(result, Completed)
        return result

    return asyncio.run(run())


def test_export_import_timeline_and_delete(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    home = tmp_path / "a"
    result = ran(home)
    branch = result.thread.branch
    assert main(["--store", str(home), "export", branch]) == 0
    exported = capsysbinary.readouterr().out
    assert exported.startswith(b'{"branch_id"')
    dump = tmp_path / "branch.jsonl"
    dump.write_bytes(exported)
    other = tmp_path / "b"
    # Import replays every request against the artifacts already in the target store.
    shutil.copytree(home / "artifacts", other / "artifacts")
    assert main(["--store", str(other), "import", str(dump)]) == 0
    assert capsysbinary.readouterr().out.strip() == branch.encode()
    assert main(["--store", str(other), "export", branch]) == 0
    assert capsysbinary.readouterr().out == exported
    assert main(["--store", str(home), "timeline", result.thread.id]) == 0
    assert b'"type":"user_input"' in capsysbinary.readouterr().out
    assert main(["--store", str(home), "delete", result.thread.id]) == 0
    capsysbinary.readouterr()
    assert main(["--store", str(home), "export", branch]) == 1
    usage_error = 2
    assert main(["--store", str(home), "delete"]) == usage_error


def test_gc_sweeps_only_unreferenced_old_artifacts(tmp_path: Path) -> None:
    home = tmp_path / "a"
    ran(home)
    kept = list((home / "artifacts").glob("sha256/*/*"))
    assert kept
    for path in kept:
        os.utime(path, (OLD, OLD))
    orphan = home / "artifacts" / "sha256" / "ab" / ("ab" + "0" * 62)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x")
    os.utime(orphan, (OLD, OLD))
    fresh = home / "artifacts" / "sha256" / "cd" / ("cd" + "0" * 62)
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(b"y")
    assert main(["--store", str(home), "gc"]) == 0
    assert not orphan.exists()
    assert fresh.exists()
    assert all(p.exists() for p in kept)


def test_dev_loads_the_host_and_prints_its_webhook_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = tmp_path / "myapp.py"
    module.write_text(
        "from threads import agent, scripted_model, sqlite\n"
        "from threads.host import host\n"
        "from threads.secrets import secret\n"
        "from threads.slack import slack\n"
        "bot = agent(model=scripted_model({'responses': []}))\n"
        "app = host(store=sqlite(':memory:'), agents={'support': bot}, channels={'slack': slack(\n"
        "    signing_secret=secret('S'), bot_token=secret('T'), agent='support')})\n"
    )
    served: list[tuple[Host, str, int]] = []

    def record(loaded: Host, bind: str, port: int) -> None:
        served.append((loaded, bind, port))

    monkeypatch.setattr(serve, "serve", record)
    assert main(["dev", str(module)]) == 0
    assert "webhook: http://localhost:8787/channels/slack/events" in capsys.readouterr().out
    ((loaded, bind, port),) = served
    assert (isinstance(loaded, Host), bind, port) == (True, "127.0.0.1", 8787)


def test_start_refuses_a_host_whose_agent_uses_dev_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = tmp_path / "devapp.py"
    module.write_text(
        "from threads import agent, scripted_model, sqlite\n"
        "from threads.dev import dev_sandbox\n"
        "from threads.host import host\n"
        f"bot = agent(model=scripted_model({{'responses': []}}), "
        f"sandbox=dev_sandbox(root={str(tmp_path / 'dev')!r}))\n"
        "app = host(store=sqlite(':memory:'), agents={'support': bot})\n"
    )
    served: list[str] = []
    usage_error = 2
    monkeypatch.setattr(serve, "serve", lambda *_: served.append("served"))
    # dev is what the dev sandbox is for; start is production and refuses it.
    assert main(["start", str(module)]) == usage_error
    assert "development only" in capsys.readouterr().err
    assert served == []
    assert main(["dev", str(module)]) == 0
    assert served == ["served"]

"""The Observability guide's Python example runs as printed, against a local collector."""

import asyncio
import os
import sys
from pathlib import Path

from otel_collector_kit import collector

PYTHON = Path(__file__).resolve().parents[2]
EXAMPLE = PYTHON / "examples" / "telemetry.py"
GUIDES = PYTHON.parent / "docs" / "content" / "docs" / "(guides)"
GUIDE = GUIDES / "production" / "observability.mdx"


def test_the_guide_shows_the_example_verbatim() -> None:
    assert EXAMPLE.read_text() in GUIDE.read_text()


def test_the_example_runs_and_exports_its_turn(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            env = {**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": c.url.removesuffix("/v1/traces")}
            ran = await asyncio.create_subprocess_exec(
                sys.executable,
                str(EXAMPLE),
                cwd=tmp_path,
                env=env,
                stdout=asyncio.subprocess.PIPE,
            )
            out, _ = await ran.communicate()
            assert ran.returncode == 0
            assert out == b"2 spans exported\n"
            names = sorted(str(s["name"]) for r in c.accepted() for s in r.spans())
            assert names == ["chat scripted-1", "invoke_agent agent"]
            assert c.accepted()[0].json_body.count(b'"support-bot"') == 1

    asyncio.run(main())

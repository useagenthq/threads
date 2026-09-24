"""Every example under python/examples with an `# Output:` line runs in process and prints it
(the others need a live service, like telemetry.py's collector)."""

import asyncio
import importlib.util
import inspect
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
MARKER = "# Output: "
PRINTING = sorted(p for p in EXAMPLES.glob("*.py") if MARKER in p.read_text())


@pytest.mark.parametrize("path", PRINTING, ids=lambda p: p.name)
def test_an_example_prints_its_output_line(path: Path) -> None:
    source = path.read_text()
    want = next(
        line.removeprefix(MARKER) for line in source.splitlines() if line.startswith(MARKER)
    )
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main: object = getattr(module, "main", None)
    assert callable(main)
    got: object = main()
    if inspect.iscoroutine(got):
        got = asyncio.run(got)
    assert got == want

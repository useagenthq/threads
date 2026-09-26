# A coding agent in one call: Claude Sonnet 5, a Docker sandbox, memory, and the edits back as a
# diff. The real thing is one line, `coding_agent(workspace={"local_dir": "./app"})`.
# Needs: nothing (a scripted model and a fake sandbox stand in for Claude and Docker here, so
#        this runs with no API key, no network and no daemon).
# Run: cd python && uv run python examples/coding_agent.py
"""A coding agent in one call, with the edits coming back as a diff."""

import asyncio
import tempfile
from pathlib import Path

from pydantic import JsonValue

from threads import Completed, fake_sandbox, scripted_model, sqlite
from threads.coding import coding_agent

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def call(name: str, args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def project(root: Path) -> Path:
    """The directory you would point `local_dir` at: your project, copied into /workspace."""
    (root / "src").mkdir()
    (root / "src" / "cli.py").write_text("FLAGS = []\n")
    return root


async def main() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        answer: JsonValue = {
            "content": [{"type": "text", "text": "Added --json to the CLI; its tests pass."}],
            "stop_reason": "end_turn",
            "usage": USAGE,
        }
        coder = coding_agent(
            # Everything below stands in for the default Claude Sonnet 5 and docker() so the
            # example runs anywhere; drop these two arguments and it is the real thing.
            model=scripted_model(
                {
                    "responses": [
                        call("read", {"path": "src/cli.py"}, "c1"),
                        call(
                            "edit",
                            {"path": "src/cli.py", "old_string": "[]", "new_string": '["--json"]'},
                            "c2",
                        ),
                        call("bash", {"command": "pytest 2>&1 | tail -5"}, "c3"),
                        answer,
                    ]
                }
            ),
            sandbox=fake_sandbox({"tools": {"pytest": {"output": "1 passed\n"}}}),
            workspace={"local_dir": str(project(Path(tmp)))},
        )
        result = await coder.run(
            "Add a --json flag to the CLI and run its tests", store=sqlite(":memory:")
        )
        # The real agent ends its answer with a unified diff of everything it changed, which you
        # apply with `git apply`. Your files on disk are untouched: /workspace was a copy.
        return result.output if isinstance(result, Completed) else result.status


if __name__ == "__main__":
    print(asyncio.run(main()))
# Output: Added --json to the CLI; its tests pass.

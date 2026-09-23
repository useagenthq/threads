"""The shared built-in tool vector (spec/conformance/vectors/tool-inputs.json): each generated
input model accepts and rejects exactly as authored, and the embedded catalog is the golden."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from threads._generated import tools_v1

SPEC = Path(__file__).resolve().parents[2] / "spec"
VECTOR: dict[str, JsonValue] = json.loads(
    (SPEC / "conformance" / "vectors" / "tool-inputs.json").read_text(encoding="utf-8")
)
CATALOG = (SPEC / "schema" / "tools.v1.catalog.json").read_bytes()
MODELS = {
    "bash": tools_v1.BashInput,
    "computer": tools_v1.ComputerInput,
    "computer_screenshot": tools_v1.ComputerScreenshotInput,
    "edit": tools_v1.EditInput,
    "forget_memory": tools_v1.ForgetMemoryInput,
    "git_clone": tools_v1.GitCloneInput,
    "git_fetch": tools_v1.GitFetchInput,
    "git_push": tools_v1.GitPushInput,
    "glob": tools_v1.GlobInput,
    "grep": tools_v1.GrepInput,
    "handoff": tools_v1.HandoffInput,
    "load_skill": tools_v1.LoadSkillInput,
    "ls": tools_v1.LsInput,
    "lsp": tools_v1.LspInput,
    "notebook_edit": tools_v1.NotebookEditInput,
    "open_pull_request": tools_v1.OpenPullRequestInput,
    "read": tools_v1.ReadInput,
    "read_tool_result": tools_v1.ReadToolResultInput,
    "save_memory": tools_v1.SaveMemoryInput,
    "search_knowledge": tools_v1.SearchKnowledgeInput,
    "search_memory": tools_v1.SearchMemoryInput,
    "send_message": tools_v1.SendMessageInput,
    "spawn_agent": tools_v1.SpawnAgentInput,
    "team_task_claim": tools_v1.TeamTaskClaimInput,
    "team_task_create": tools_v1.TeamTaskCreateInput,
    "team_task_update": tools_v1.TeamTaskUpdateInput,
    "todo_write": tools_v1.TodoWriteInput,
    "web_fetch": tools_v1.WebFetchInput,
    "web_search": tools_v1.WebSearchInput,
    "write": tools_v1.WriteInput,
}


def _cases() -> list[tuple[str, JsonValue, bool]]:
    cases = VECTOR["cases"]
    assert isinstance(cases, list)
    out: list[tuple[str, JsonValue, bool]] = []
    for c in cases:
        assert isinstance(c, dict)
        tool, valid = c["tool"], c["valid"]
        assert isinstance(tool, str)
        assert isinstance(valid, bool)
        out.append((tool, c["input"], valid))
    return out


@pytest.mark.parametrize(("tool", "value", "valid"), _cases())
def test_input_accepts_exactly_the_valid_cases(tool: str, value: JsonValue, valid: bool) -> None:
    try:
        MODELS[tool].model_validate(value)
    except ValidationError:
        assert not valid
    else:
        assert valid


def test_embedded_catalog_is_the_golden() -> None:
    assert tools_v1.TOOL_CATALOG.encode() == CATALOG
    assert hashlib.sha256(CATALOG).hexdigest() == VECTOR["catalog_sha256"]


def test_every_catalog_tool_has_a_model() -> None:
    listed: JsonValue = json.loads(CATALOG)
    assert isinstance(listed, list)
    names = [e["name"] for e in listed if isinstance(e, dict)]
    assert names == sorted(MODELS)

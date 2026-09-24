"""tool_search matching against the shared vectors (spec/conformance/vectors), properties of the
fold, and the lint that keeps runtime case and whitespace handling out of it."""

import json
import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue, TypeAdapter

from threads._generated.fold_table import SEPARATORS, UNICODE_VERSION
from threads.adapters.models.render import parse
from threads.tools import tool_search
from threads.tools.tool_search import fold, search, terms, tokens

SPEC = Path(__file__).resolve().parents[3] / "spec"
VECTORS = SPEC / "conformance" / "vectors"
_DOC: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def _cases(name: str) -> list[dict[str, JsonValue]]:
    doc = _DOC.validate_json((VECTORS / name).read_bytes())
    cases = doc["cases"]
    assert isinstance(cases, list)
    return [c for c in cases if isinstance(c, dict)]


def test_the_table_is_the_pinned_unicode_version() -> None:
    doc = _DOC.validate_json((VECTORS / "unicode-fold.json").read_bytes())
    assert doc["unicode_version"] == UNICODE_VERSION == "15.0.0"


@pytest.mark.parametrize("case", _cases("unicode-fold.json"), ids=lambda c: repr(c["input"]))
def test_fold_and_tokens_match_the_vector(case: dict[str, JsonValue]) -> None:
    text = case["input"]
    assert isinstance(text, str)
    assert fold(text) == case["fold"]
    assert tokens(text) == case["tokens"]


@pytest.mark.parametrize("case", _cases("query-split.json"), ids=lambda c: repr(c["query"]))
def test_query_split_matches_the_vector(case: dict[str, JsonValue]) -> None:
    query = case["query"]
    assert isinstance(query, str)
    assert terms(query) == case["terms"]


def _pair(tool: JsonValue) -> tuple[str, str]:
    assert isinstance(tool, dict)
    name, description = tool["name"], tool["description"]
    assert isinstance(name, str)
    assert isinstance(description, str)
    return name, description


@pytest.mark.parametrize("case", _cases("tool-search.json"), ids=lambda c: repr(c["query"]))
def test_search_matches_the_vector(case: dict[str, JsonValue]) -> None:
    query, limit, deferred, loaded = case["query"], case["limit"], case["deferred"], case["loaded"]
    assert isinstance(query, str)
    assert isinstance(limit, int)
    assert isinstance(deferred, list)
    assert isinstance(loaded, list)
    found = search(query, limit, [_pair(t) for t in deferred], [str(n) for n in loaded])
    assert list(found.lines) == case["lines"]
    assert list(found.loaded) == case["loads"]


@pytest.mark.parametrize("case", _cases("provider-tools.json"), ids=lambda c: str(c["name"]))
def test_provider_tools_match_the_vector(case: dict[str, JsonValue]) -> None:
    lines = case["request"]
    assert isinstance(lines, list)
    body = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
    tools = [json.loads(t.model_dump_json(exclude_unset=True)) for t in parse(body).tools]
    assert tools == case["tools"]
    assert all("deferred" not in t for t in tools)


@given(st.text())
def test_tokens_are_non_empty_runs_of_the_fold(text: str) -> None:
    folded = fold(text)
    assert all(t and t in folded and not any(ord(c) in SEPARATORS for c in t) for t in tokens(text))


@given(st.text())
def test_terms_hold_no_separator(text: str) -> None:
    assert all(t and not any(ord(c) in SEPARATORS for c in t) for t in terms(text))


def test_only_separators_is_no_match() -> None:
    found = search(" , ﻿\u001f", 5, [("mcp__a__x", "X.")], [])
    assert found.lines == (tool_search.NO_MATCH,)
    assert found.loaded == ()


def test_matching_never_uses_the_runtime_case_or_whitespace() -> None:
    source = Path(tool_search.__file__).read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", source)
    banned = (r"\.lower\(", r"casefold", r"\.strip\(", r"\.split\(\)", r"isspace", r"re\.split")
    for pattern in banned:
        assert re.search(pattern, code) is None, pattern

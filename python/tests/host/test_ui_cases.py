"""Every `ui` case of spec/conformance/cases, as spec/conformance/README.md "ui" runs it: the
host's frames byte for byte, each valid under its pinned protocol schema, the stream accepted by
the ported sequence rules, and the messages the stock client builds from it."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from corpus import CASES, cases
from fixtures.ui_fold import ai_fold
from jsonschema import Draft7Validator, Draft202012Validator
from jsonschema.protocols import Validator
from pydantic import JsonValue
from ui_case_kit import Case, expected, line, loaded, serve

from threads.host.ui.ag_ui_fold import AgUiFold
from threads.host.ui.agui_sequence import check

SCHEMAS = Path(__file__).resolve().parents[3] / "spec" / "schema" / "ui"
AG_UI_SHA256 = "4b5c93226838a0e72d88e6c5df20633c686c49fcb75d9be2815c6cbf9e48e71a"
AG_UI: Validator = Draft202012Validator(
    json.loads((SCHEMAS / "ag-ui-1.0.schema.json").read_bytes())
)
AI_SDK: Validator = Draft7Validator(json.loads((SCHEMAS / "ai-sdk-ui.v1.schema.json").read_bytes()))
AG_KEYS = ("id", "role", "content", "toolCallId", "toolCalls")

type Chunk = Mapping[str, JsonValue]


def test_the_vendored_ag_ui_schema_is_the_pinned_one() -> None:
    digest = hashlib.sha256((SCHEMAS / "ag-ui-1.0.schema.json").read_bytes()).hexdigest()
    assert digest == AG_UI_SHA256


def ai_sdk_problems(chunks: Sequence[Chunk]) -> list[str]:
    """processUIMessageStream's open/close rules: a text or reasoning part opens once, and its
    delta and end need it open."""
    open_: set[str] = set()
    seen: set[str] = set()
    out: list[str] = []
    for c in chunks:
        kind, pid = str(c.get("type")), str(c.get("id"))
        if kind in ("text-start", "reasoning-start"):
            if pid in seen:
                out.append(f"{kind} twice for {pid}")
            open_.add(pid)
            seen.add(pid)
        elif kind in ("text-delta", "reasoning-delta", "text-end", "reasoning-end"):
            if pid not in open_:
                out.append(f"{kind} for an unopened {pid}")
            if kind.endswith("-end"):
                open_.discard(pid)
    return out


def ag_projection(messages: Sequence[Mapping[str, JsonValue]]) -> list[JsonValue]:
    return [{k: m[k] for k in AG_KEYS if k in m} for m in messages]


@pytest.mark.parametrize("name", cases("ui"))
def test_ui_case(name: str) -> None:
    case = loaded(CASES / name)
    served = serve(case)
    lines = [line(f) for f in served.frames]
    if served.done and case.protocol == "ai-sdk":
        lines.append(line("[DONE]"))
    want = (case.path / "frames.jsonl").read_text().split("\n")[:-1]
    assert lines == want
    chunks = [f.data for f in served.frames]
    validator = AI_SDK if case.protocol == "ai-sdk" else AG_UI
    for c in chunks:
        assert validator.is_valid(dict(c)), c
    if case.protocol == "ai-sdk":
        prior = _received(case) if "after" in case.input else []
        stream = [*prior, *chunks]
        assert ai_sdk_problems(stream) == []
        folded = ai_fold([dict(c) for c in stream])
        assert folded == expected(case)
        return
    check(chunks)
    fold = AgUiFold()
    for c in chunks:
        fold.apply(c)
    assert ag_projection(fold.messages) == expected(case)


def _received(case: Case) -> list[Chunk]:
    """An AI SDK client resuming at a cursor has the canonical stream through it."""
    whole = serve(case, without_after=True).frames
    upto = next(i for i, f in enumerate(whole) if f.id == case.input["after"])
    return [f.data for f in whole[: upto + 1]]

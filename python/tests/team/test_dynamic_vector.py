"""The dynamic-agent vector (spec/conformance/vectors/dynamic.json): the framework set F, the exact
block bytes, and resolve_definition on every input, byte for byte against the reference."""

import json
from pathlib import Path
from typing import Final

import pytest
from pydantic import JsonValue, TypeAdapter

from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.team.dynamic import (
    INSTRUCTIONS_CAP,
    KEPT,
    LABEL_MAX,
    Template,
    block,
    resolve_definition,
)

FILE = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors" / "dynamic.json"
type Obj = dict[str, JsonValue]
_OBJ: TypeAdapter[Obj] = TypeAdapter(Obj)
_STRS: TypeAdapter[list[str]] = TypeAdapter(list[str])
DOC: Final = _OBJ.validate_python(json.loads(FILE.read_text(encoding="utf-8")))
_ENTRIES: TypeAdapter[list[Obj]] = TypeAdapter(list[Obj])


def test_constants() -> None:
    assert _STRS.validate_python(DOC["kept"]) == sorted(KEPT)
    assert DOC["instructions_cap_bytes"] == INSTRUCTIONS_CAP
    assert DOC["label_max"] == LABEL_MAX


@pytest.mark.parametrize("e", _ENTRIES.validate_python(DOC["blocks"]))
def test_block(e: Obj) -> None:
    assert block(str(e["starter"]), str(e["text"])) == e["block"]


def _optional(inp: Obj, key: str) -> str | None:
    value = inp.get(key)
    return None if value is None else str(value)


def _got(template: Template | None, inp: Obj) -> Obj:
    tools = inp.get("tools")
    resolved = resolve_definition(
        template,
        label=_optional(inp, "label"),
        instructions=_optional(inp, "instructions"),
        tools=None if tools is None else tuple(_STRS.validate_python(tools)),
        model=_optional(inp, "model"),
    )
    if not isinstance(resolved, Ok):
        return {"error": resolved.error.to_json()}
    out: Obj = {}
    if resolved.value.label is not None:
        out["label"] = resolved.value.label
    if resolved.value.define is not None:
        out["define"] = to_json(resolved.value.define)
    return {"ok": out}


@pytest.mark.parametrize("e", _ENTRIES.validate_python(DOC["resolve"]))
def test_resolve(e: Obj) -> None:
    raw = e["template"]
    template = None
    if raw is not None:
        t = _OBJ.validate_python(raw)
        template = Template(
            tuple(_STRS.validate_python(t["tools"])), tuple(_STRS.validate_python(t["models"]))
        )
    assert _got(template, _OBJ.validate_python(e["input"])) == e["expect"]

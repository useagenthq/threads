"""The same agent pins the same thread_started and config_hash in both languages
(spec/schema/README.md, "The pinned config"): the shared vector pins the bytes."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue, TypeAdapter

from threads import Agent, agent, scripted_model
from threads.agents.bindings import DEFAULT_PERMISSIONS
from threads.log import Context, Permissions, Retry
from threads.loop.defaults import CONTEXT, RETRY

if TYPE_CHECKING:
    from threads.agents.factory import AgentOptions

type Obj = dict[str, JsonValue]
_OBJ: TypeAdapter[Obj] = TypeAdapter(Obj)
_OBJS: TypeAdapter[list[Obj]] = TypeAdapter(list[Obj])
_TEXT: TypeAdapter[str] = TypeAdapter(str)
VECTOR = _OBJ.validate_json(
    (Path(__file__).resolve().parents[3] / "spec/conformance/vectors/agent-pins.json").read_bytes()
)


def _section(d: Obj, key: str, default: JsonValue) -> Obj:
    """Python takes a complete section: the default one with the vector's fields replaced."""
    return {**_OBJ.validate_python(default), **_OBJ.validate_python(d[key])}


def _build(d: Obj) -> Agent[None, object]:
    options: AgentOptions = {
        "name": _TEXT.validate_python(d["name"]),
        "instructions": _TEXT.validate_python(d["instructions"]),
        "model": scripted_model({"responses": []}),
    }
    if "permissions" in d:
        default = DEFAULT_PERMISSIONS.model_dump(mode="json")
        options["permissions"] = Permissions.model_validate(_section(d, "permissions", default))
    if "retry" in d:
        options["retry"] = Retry.model_validate(_section(d, "retry", RETRY.model_dump(mode="json")))
    if "context" in d:
        default = CONTEXT.model_dump(mode="json")
        options["context"] = Context.model_validate(_section(d, "context", default))
    if "subagents" in d:
        options["subagents"] = [_build(s) for s in _OBJS.validate_python(d["subagents"])]
    if "team" in d:
        return agent(**options, team=[_build(s) for s in _OBJS.validate_python(d["team"])])
    return agent(**options)


@pytest.mark.parametrize(
    "case", _OBJS.validate_python(VECTOR["cases"]), ids=lambda c: str(c["name"])
)
def test_the_agent_pins_the_vector(case: Obj) -> None:
    started, _ = _build(_OBJ.validate_python(case["agent"])).definition.pin()
    assert started == case["thread_started"]

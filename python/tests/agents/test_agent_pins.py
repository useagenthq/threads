"""The same agent pins the same thread_started and config_hash in both languages
(spec/schema/README.md, "The pinned config"): the shared vector pins the bytes."""

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, override

import pytest
from pydantic import JsonValue, TypeAdapter

from threads import (
    Agent,
    RunContext,
    Skill,
    agent,
    extension,
    fake_sandbox,
    local_memory,
    scripted_model,
)
from threads.agents.bindings import DEFAULT_PERMISSIONS
from threads.hooks.extension import Extension
from threads.log import Budget, Context, Event, ModelRef, Permissions, Retry
from threads.loop.defaults import CONTEXT, RETRY
from threads.loop.model import ModelInfo
from threads.loop.scripted import SCRIPTED_INFO, ScriptedModel

if TYPE_CHECKING:
    from threads.agents.factory import AgentOptions
    from threads.hooks.types import Hooks

type Obj = dict[str, JsonValue]
_OBJ: TypeAdapter[Obj] = TypeAdapter(Obj)
_OBJS: TypeAdapter[list[Obj]] = TypeAdapter(list[Obj])
_TEXT: TypeAdapter[str] = TypeAdapter(str)
_TEXTS: TypeAdapter[list[str]] = TypeAdapter(list[str])
_INT: TypeAdapter[int] = TypeAdapter(int)
_STYLES: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])
_UNKNOWN: TypeAdapter[Literal["upper_bound", "stop"]] = TypeAdapter(Literal["upper_bound", "stop"])
_WRITE: TypeAdapter[Literal["deny", "ask", "allow_principal", "allow"]] = TypeAdapter(
    Literal["deny", "ask", "allow_principal", "allow"]
)
VECTOR = _OBJ.validate_json(
    (Path(__file__).resolve().parents[3] / "spec/conformance/vectors/agent-pins.json").read_bytes()
)


class _Declared(ScriptedModel):
    """A scripted model under another name, declaring a price and a cache lifetime."""

    def __init__(self, info: ModelInfo) -> None:
        super().__init__([], {})
        self._declared = info

    @property
    @override
    def info(self) -> ModelInfo:
        return self._declared


def _model(d: Obj) -> ScriptedModel:
    name = _TEXT.validate_python(d["name"])
    limits = SCRIPTED_INFO.limits.model_copy(update={"name": name})
    if "price" in d:
        priced = {**limits.model_dump(mode="json"), "price": d["price"]}
        limits = type(limits).model_validate(priced)
    cache = {"ttl_ms": _INT.validate_python(d["cache_ttl_ms"])} if "cache_ttl_ms" in d else "none"
    ref = ModelRef(provider="scripted", name=name)
    return _Declared(replace(SCRIPTED_INFO, model=ref, limits=limits, cache=cache))


async def _session_end(_ctx: RunContext[None]) -> None:
    return None


async def _notification(_event: Event, _ctx: RunContext[None]) -> None:
    return None


async def _observe(_event: Event) -> None:
    return None


def _extension(d: Obj) -> Extension:
    hooks: Hooks = {}
    for name in _TEXTS.validate_python(d.get("hooks", [])):
        if name == "session_end":
            hooks["session_end"] = _session_end
        else:
            assert name == "notification", name
            hooks["notification"] = _notification
    observers = dict.fromkeys(_TEXTS.validate_python(d.get("observers", [])), _observe)
    ext = extension(
        name=_TEXT.validate_python(d["name"]),
        instructions=_TEXT.validate_python(d.get("instructions", "")),
        hooks=hooks,
        on=observers,
    )
    if "hook_timeout_ms" in d:
        return replace(ext, hook_timeout_ms=_INT.validate_python(d["hook_timeout_ms"]))
    return ext


def _section(d: Obj, key: str, default: JsonValue) -> Obj:
    """Python takes a complete section: the default one with the vector's fields replaced."""
    return {**_OBJ.validate_python(default), **_OBJ.validate_python(d[key])}


def _settings(d: Obj, options: "AgentOptions") -> None:
    if "permissions" in d:
        default = DEFAULT_PERMISSIONS.model_dump(mode="json")
        options["permissions"] = Permissions.model_validate(_section(d, "permissions", default))
    if "retry" in d:
        options["retry"] = Retry.model_validate(_section(d, "retry", RETRY.model_dump(mode="json")))
    if "context" in d:
        default = CONTEXT.model_dump(mode="json")
        options["context"] = Context.model_validate(_section(d, "context", default))
    if "budget" in d:
        options["budget"] = Budget.model_validate(d["budget"])
    if "on_unknown_usage" in d:
        options["on_unknown_usage"] = _UNKNOWN.validate_python(d["on_unknown_usage"])
    if "output_styles" in d:
        options["output_styles"] = _STYLES.validate_python(d["output_styles"])


def _parts(d: Obj, options: "AgentOptions") -> None:
    if "fallback" in d:
        options["fallback"] = [_model(f) for f in _OBJS.validate_python(d["fallback"])]
    if "extensions" in d:
        options["extensions"] = [_extension(e) for e in _OBJS.validate_python(d["extensions"])]
    if "sandbox" in d:
        options["sandbox"] = fake_sandbox()
    if "memory_write" in d:
        options["memory"] = local_memory()
        options["memory_write"] = _WRITE.validate_python(d["memory_write"])
    if "skills" in d:
        skills = _OBJS.validate_python(d["skills"])
        options["skills"] = [
            Skill(*(_TEXT.validate_python(s[k]) for k in ("name", "description", "body")))
            for s in skills
        ]
    if "subagents" in d:
        options["subagents"] = [_build(s) for s in _OBJS.validate_python(d["subagents"])]
    if "handoffs" in d:
        options["handoffs"] = [_build(s) for s in _OBJS.validate_python(d["handoffs"])]


def _build(d: Obj) -> Agent[None, object]:
    model = (
        _model(_OBJ.validate_python(d["model"]))
        if "model" in d
        else scripted_model({"responses": []})
    )
    options: AgentOptions = {
        "name": _TEXT.validate_python(d["name"]),
        "instructions": _TEXT.validate_python(d["instructions"]),
        "model": model,
    }
    _settings(d, options)
    _parts(d, options)
    if "team" in d:
        return agent(**options, team=[_build(s) for s in _OBJS.validate_python(d["team"])])
    return agent(**options)


@pytest.mark.parametrize(
    "case", _OBJS.validate_python(VECTOR["cases"]), ids=lambda c: str(c["name"])
)
def test_the_agent_pins_the_vector(case: Obj) -> None:
    definition = _build(_OBJ.validate_python(case["agent"])).definition
    if case["team_member"] is True:
        definition = replace(definition, in_team=True)
    started, _ = definition.pin()
    assert started == case["thread_started"]

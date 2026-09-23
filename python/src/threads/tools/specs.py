"""The built-in tools' pinned specs: names, descriptions and input schemas come
from the shared catalog (spec/schema/tools.v1.catalog.json, generated into `tools_v1`), and the
generated input models parse the model's arguments. Only the effect class is decided here."""

import json
from collections.abc import Mapping
from typing import Final, TypedDict

from pydantic import JsonValue, TypeAdapter

from threads._generated import tools_v1
from threads._strict_model import StrictModel
from threads.log import EffectClass, ToolSpec


class _Entry(TypedDict):
    name: str
    description: str
    input_schema: dict[str, JsonValue]


_ENTRIES: Final = TypeAdapter(list[_Entry]).validate_python(json.loads(tools_v1.TOOL_CATALOG))

MODELS: Final[Mapping[str, type[StrictModel]]] = {
    "bash": tools_v1.BashInput,
    "edit": tools_v1.EditInput,
    "glob": tools_v1.GlobInput,
    "grep": tools_v1.GrepInput,
    "ls": tools_v1.LsInput,
    "read": tools_v1.ReadInput,
    "read_tool_result": tools_v1.ReadToolResultInput,
    "write": tools_v1.WriteInput,
}
_EFFECTS: Final[Mapping[str, EffectClass]] = {
    "bash": "sandbox_local",
    "edit": "sandbox_local",
    "glob": "read_only",
    "grep": "read_only",
    "ls": "read_only",
    "read": "read_only",
    "read_tool_result": "read_only",
    "write": "sandbox_local",
}
HOST: Final = frozenset({"read_tool_result"})
"""Built-ins that run on the host: offered with or without a sandbox."""
NAMES: Final = frozenset(MODELS)
SANDBOXED: Final = NAMES - HOST


def specs(*, sandbox: bool, egress_denied: bool) -> tuple[ToolSpec, ...]:
    """The pinned built-ins, sorted by name. bash is sandbox_local only under deny-all egress;
    with any outbound path a command may change state elsewhere."""
    out: list[ToolSpec] = []
    for entry in _ENTRIES:
        name = entry["name"]
        if name not in NAMES:
            continue  # an framework tool, pinned with its feature
        if name not in HOST and not sandbox:
            continue
        effect = "unguarded" if name == "bash" and not egress_denied else _EFFECTS[name]
        out.append(ToolSpec.model_validate({**entry, "effect_class": effect}))
    return tuple(out)

"""The built-in sandbox tools' pinned names, input models and effect classes (). Each input model is the tool's one schema: its JSON Schema is the pinned
`input_schema`, and the same model parses the model's arguments."""

from collections.abc import Mapping
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from threads.log import EffectClass, ToolSpec

MAX_TIMEOUT_MS: Final = 600_000


class _Input(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True, frozen=True)


class BashInput(_Input):
    command: str = Field(min_length=1, description="The shell command, run with bash -c.")
    timeout_ms: int | None = Field(
        default=None, ge=1, le=MAX_TIMEOUT_MS, description="Deadline; default 120000."
    )


class ReadInput(_Input):
    path: str = Field(min_length=1, description="A file, relative to /workspace or absolute.")
    offset: int = Field(default=0, ge=0, description="The first line to show, from 0.")
    limit: int = Field(default=2000, ge=1, description="At most this many lines.")


class WriteInput(_Input):
    path: str = Field(min_length=1)
    content: str
    expected_sha256: str | None = Field(
        default=None, description="Write only if the file's current sha256 is this."
    )


class EditInput(_Input):
    path: str = Field(min_length=1)
    old_string: str = Field(min_length=1, description="Exact text; must occur once.")
    new_string: str
    replace_all: bool = False
    expected_sha256: str | None = None


class LsInput(_Input):
    path: str = Field(default=".", min_length=1)


class GlobInput(_Input):
    pattern: str = Field(min_length=1, description="A bash globstar pattern, like **/*.py.")
    path: str = Field(default=".", min_length=1)


class GrepInput(_Input):
    pattern: str = Field(min_length=1, description="An extended regular expression.")
    path: str = Field(default=".", min_length=1)
    glob: str | None = Field(default=None, description="Only files whose name matches this.")


_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

_CATALOG: Final[Mapping[str, tuple[str, type[_Input], EffectClass]]] = {
    "bash": ("Run a shell command in the sandbox.", BashInput, "sandbox_local"),
    "edit": ("Replace exact text in a file.", EditInput, "sandbox_local"),
    "glob": ("List files matching a glob pattern.", GlobInput, "read_only"),
    "grep": ("Search file contents with a regular expression.", GrepInput, "read_only"),
    "ls": ("List a directory.", LsInput, "read_only"),
    "read": ("Read a text file with line numbers.", ReadInput, "read_only"),
    "write": ("Write a whole file.", WriteInput, "sandbox_local"),
}
"""By name, sorted: built-ins come first in line 0, sorted by name."""

NAMES: Final = frozenset(_CATALOG)


def input_model(name: str) -> type[_Input]:
    return _CATALOG[name][1]


def specs(*, egress_denied: bool) -> tuple[ToolSpec, ...]:
    """The pinned specs. bash is sandbox_local only under deny-all egress; with any outbound
    path a command may change state elsewhere, so it is unguarded."""
    out: list[ToolSpec] = []
    for name, (description, model, declared) in _CATALOG.items():
        effect = "unguarded" if name == "bash" and not egress_denied else declared
        schema = _OBJECT.validate_python(model.model_json_schema())
        out.append(
            ToolSpec(name=name, description=description, input_schema=schema, effect_class=effect)
        )
    return tuple(out)

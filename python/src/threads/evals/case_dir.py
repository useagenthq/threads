"""One saved case directory, read and parsed at the boundary (spec/conformance/case.schema.json,
spec/schema/eval.v1.schema.json). The case's files are user input: anything malformed makes the
case an `error`, never a raise."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, JsonValue, StrictInt, StrictStr, TypeAdapter, ValidationError

from threads._generated.eval_v1 import (
    CaseLine0,
    CaseOffline,
    CaseSnapshot,
    EventMatcher,
    ExtensionScript,
    Rubric,
    SandboxResult,
    SandboxResults,
)
from threads._strict_model import StrictModel
from threads.evals.compare import Wire
from threads.log import Event
from threads.loop.scripted import scripted_model
from threads.reduce.handlers import to_json
from threads.result import Err, Ok

IMPL: Final = "threads-py"


class _Clock(StrictModel):
    now: Annotated[StrictInt, Field(ge=0)]


class _Input(StrictModel):
    text: StrictStr | None = None


class _Expect(StrictModel):
    must: Annotated[Sequence[EventMatcher], Field(min_length=1)]
    expect: Sequence[EventMatcher] = ()


class CaseFile(StrictModel):
    """case.json as the runner reads it: a saved (stub kind) case."""

    name: StrictStr
    family: StrictStr
    kind: Literal["stub"]
    description: StrictStr
    clock: _Clock
    model_script: Literal["model.json"] | None = None
    sandbox_script: Literal["sandbox.json"] | None = None
    stub_script: Literal["stubs.json"] | None = None
    extension_script: Literal["extensions.json"] | None = None
    input: _Input | None = None
    expect: _Expect
    rubric: Rubric | None = None
    snapshot: CaseSnapshot | None = None
    offline: CaseOffline | None = None
    line0: CaseLine0 | None = None


@dataclass(frozen=True, slots=True)
class CaseSandbox:
    """sandbox.json: v2 results, or the v1 shape (one entry per tool name)."""

    results: tuple[SandboxResult, ...] = ()
    v1: Mapping[str, Mapping[str, JsonValue]] | None = None


@dataclass(frozen=True, slots=True)
class CaseDir:
    name: str
    meta: CaseFile
    log: bytes
    artifacts: tuple[bytes, ...]
    model: JsonValue
    sandbox: CaseSandbox | None
    stubs: JsonValue
    extensions: ExtensionScript | None
    line0: bytes | None
    recorded: tuple[Event, ...] | None
    """The recorded turn, in full; None for a case saved before full events were kept."""
    matchers: tuple[Wire, ...] | None
    """An older case's `appended` matchers; None when its expected file has no `appended`."""

    @property
    def must(self) -> tuple[Wire, ...]:
        return tuple(_wire(m) for m in self.meta.expect.must)


type Read = Ok[JsonValue] | Err[str]

_JSON: Final = TypeAdapter[JsonValue](JsonValue)
_EVENTS: Final = TypeAdapter[list[Event]](list[Event])
_V1: Final = TypeAdapter[dict[str, dict[str, JsonValue]]](dict[str, dict[str, JsonValue]])


def _wire(m: EventMatcher) -> Wire:
    wire = to_json(m)
    return wire if isinstance(wire, dict) else {}


def _read(folder: Path, file: str) -> Read:
    path = folder / file
    if not path.exists():
        return Err(f"{file} is missing")
    try:
        return Ok(_JSON.validate_json(path.read_bytes()))
    except ValidationError as error:
        return Err(f"{file}: {error.errors()[0]['msg']}")


def _model[T: StrictModel](read: Read, model: type[T], file: str) -> Ok[T] | Err[str]:
    if isinstance(read, Err):
        return read
    try:
        return Ok(model.model_validate_json(json.dumps(read.value)))
    except ValidationError as error:
        return Err(f"{file}: {error.errors()[0]['msg']}")


def _sandbox(folder: Path, meta: CaseFile) -> Ok[CaseSandbox | None] | Err[str]:
    if meta.sandbox_script is None:
        return Ok(None)
    read = _read(folder, "sandbox.json")
    if isinstance(read, Err):
        return read
    v2 = _model(read, SandboxResults, "sandbox.json")
    if isinstance(v2, Ok):
        return Ok(CaseSandbox(results=tuple(v2.value.results)))
    tools = read.value.get("tools") if isinstance(read.value, dict) else None
    try:
        v1 = _V1.validate_python(tools, strict=True)
    except ValidationError:
        return Err("sandbox.json: neither v2 results nor a v1 tools map")
    if not all(isinstance(t.get("output"), str) for t in v1.values()):
        return Err("sandbox.json: a v1 tool needs an output string")
    return Ok(CaseSandbox(v1=v1))


def _appended(
    read: Read,
) -> Ok[tuple[tuple[Event, ...] | None, tuple[Wire, ...] | None]] | Err[str]:
    """The recorded turn: full events, or the matchers an older save_case wrote; neither for an
    old Python case, whose expected file keeps no `appended`."""
    if isinstance(read, Err):
        return read
    if isinstance(read.value, dict) and "appended" not in read.value:
        return Ok((None, None))
    items = read.value.get("appended", []) if isinstance(read.value, dict) else None
    if not isinstance(items, list):
        return Err("expected.json: appended is not a list")
    if items:
        try:
            return Ok((tuple(_EVENTS.validate_json(json.dumps(items))), ()))
        except ValidationError:
            pass
    matchers: list[Wire] = []
    for item in items:
        try:
            matchers.append(_wire(EventMatcher.model_validate_json(json.dumps(item))))
        except ValidationError:
            return Err("expected.json: appended holds neither events nor matchers")
    return Ok((None, tuple(matchers)))


def _own(folder: Path, name: str) -> str:
    """A file save_case writes once per implementation: this one's name, else the shared one."""
    stem, dot, ext = name.partition(".")
    mine = f"{stem}.{IMPL}{dot}{ext}"
    return mine if (folder / mine).exists() else name


def _optional(folder: Path, file: str | None) -> Read:
    return Ok(None) if file is None else _read(folder, file)


@dataclass(frozen=True, slots=True)
class _Scripts:
    model: JsonValue
    sandbox: CaseSandbox | None
    stubs: JsonValue
    extensions: ExtensionScript | None


def _model_script(folder: Path, meta: CaseFile) -> Read:
    model = _optional(folder, meta.model_script)
    if isinstance(model, Err) or model.value is None:
        return model
    if not isinstance(model.value, dict):
        return Err("model.json: not a model script")
    try:
        scripted_model(model.value)
    except (ValueError, ValidationError) as error:
        return Err(f"model.json: {error}")
    return model


def _scripts(folder: Path, meta: CaseFile) -> Ok[_Scripts] | Err[str]:
    model = _model_script(folder, meta)
    if isinstance(model, Err):
        return model
    sandbox = _sandbox(folder, meta)
    if isinstance(sandbox, Err):
        return sandbox
    stubs = _optional(folder, meta.stub_script)
    if isinstance(stubs, Err):
        return stubs
    if meta.extension_script is None:
        return Ok(_Scripts(model.value, sandbox.value, stubs.value, None))
    extensions = _model(_read(folder, "extensions.json"), ExtensionScript, "extensions.json")
    if isinstance(extensions, Err):
        return extensions
    return Ok(_Scripts(model.value, sandbox.value, stubs.value, extensions.value))


def read_case(root: Path, name: str) -> Ok[CaseDir] | Err[str]:
    folder = root / name
    meta = _model(_read(folder, "case.json"), CaseFile, "case.json")
    if isinstance(meta, Err):
        return meta
    log = folder / _own(folder, "log.jsonl")
    if not log.exists():
        return Err("log.jsonl is missing")
    scripts = _scripts(folder, meta.value)
    if isinstance(scripts, Err):
        return scripts
    appended = _appended(_read(folder, _own(folder, "expected.json")))
    if isinstance(appended, Err):
        return appended
    arts = sorted((folder / "artifacts").glob("*")) if (folder / "artifacts").exists() else []
    line0 = folder / "line0.json"
    s = scripts.value
    return Ok(
        CaseDir(
            name,
            meta.value,
            log.read_bytes(),
            tuple(p.read_bytes() for p in arts),
            s.model,
            s.sandbox,
            s.stubs,
            s.extensions,
            line0.read_bytes() if line0.exists() else None,
            appended.value[0],
            appended.value[1],
        )
    )


def case_names(root: Path) -> tuple[str, ...]:
    """Every case directory under `root`, by name."""
    return tuple(sorted(d.name for d in root.iterdir() if (d / "case.json").exists()))

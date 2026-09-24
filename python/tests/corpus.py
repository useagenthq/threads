"""The shared conformance corpus (spec/conformance/cases): reading case files, and the test-kit
sandbox the `recover` cases script. Runners import this; it holds no per-case code."""

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING

from threads._json_schema import holds
from threads.log import JsonObject, ParseError, ToolSpec
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFound, NotFoundNonfinal
from threads.loop.tools import Dispatched, Invocation, Output, Termination
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import MemoryArtifacts, SqliteStore, StoredEvent, VerifiedLog, verify_export

CASES = Path(__file__).resolve().parents[2] / "spec" / "conformance" / "cases"
IMPL = "threads-py"
MATCHED_EXACTLY = ("type", "seq", "epoch", "branch_id", "critical")
_JSON: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def own(case: Path, name: str) -> Path:
    """A recover case ships one file per writer; this runner uses its own."""
    stem, dot, ext = name.partition(".")
    mine = case / f"{stem}.{IMPL}{dot}{ext}"
    return mine if mine.exists() else case / name


def load(case: Path, name: str) -> dict[str, JsonValue]:
    return _JSON.validate_json(own(case, name).read_bytes())


def cases(*kinds: str) -> list[str]:
    return sorted(d.name for d in CASES.iterdir() if load(d, "case.json")["kind"] in kinds)


def now_of(case: dict[str, JsonValue]) -> int:
    clock = case["clock"]
    assert isinstance(clock, dict)
    now = clock["now"]
    assert isinstance(now, int)
    return now


def stored_artifacts(case: Path) -> MemoryArtifacts:
    """The case's artifacts, in the store before import: import replays every request."""
    store = MemoryArtifacts()
    for path in sorted((case / "artifacts").glob("*")):
        store.put(path.read_bytes())
    return store


def schema_error(spec: ToolSpec, input: JsonObject) -> str | None:
    """Test-only: the corpus pins its tool and output schemas in the log, so the runner checks
    them with the generated models' keyword subset. Core binds schemas to Pydantic models."""
    if spec.input_schema is MISSING:
        return "invalid input: no schema"
    return None if json_schema_holds(dict(spec.input_schema), dict(input)) else "invalid input"


def full_spec(case: Path, spec: ToolSpec) -> ToolSpec:
    """A reference-form spec's full spec, from its artifact (the case's own copy)."""
    if spec.spec_ref is MISSING:
        return spec
    return ToolSpec.model_validate_json((case / "artifacts" / spec.spec_ref.sha256).read_bytes())


def json_schema_holds(schema: JsonValue, value: JsonValue) -> bool:
    try:
        return holds(schema, value)
    except TypeError:
        return False


def obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict), value
    return value


@dataclass
class Clock:
    """The injected clock: a recorded wait advances it at once; runners never sleep."""

    now: int

    def __call__(self) -> int:
        return self.now

    async def wait_until(self, when: int) -> None:
        self.now = max(self.now, when)


@dataclass
class ScriptedTools:
    """The scripted sandbox of `sandbox.json` (case.schema.json SandboxScript). A dispatch whose
    effect key the provider already executed returns that output with no new execution."""

    script: Mapping[str, JsonValue]
    clock: Clock
    case: Path | None = None
    """Where a reference-form tool's spec artifact is read from."""
    dispatches: Counter[str] = field(default_factory=Counter[str])
    new_executions: Counter[str] = field(default_factory=Counter[str])
    lookups: Counter[str] = field(default_factory=Counter[str])

    def _tool(self, name: str) -> dict[str, JsonValue]:
        return obj(obj(self.script.get("tools", {})).get(name, {}))

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        return schema_error(spec if self.case is None else full_spec(self.case, spec), input)

    async def dispatch(self, call: Invocation) -> Dispatched:
        name = call.spec.name
        tool = self._tool(name)
        self.dispatches[name] += 1
        done = obj(tool.get("executed_keys", {})).get(call.effect_key)
        if isinstance(done, str):
            return Output(done)
        self.new_executions[name] += 1
        output, is_error = tool.get("output"), tool.get("is_error", False)
        assert isinstance(output, str)
        return Output(output, is_error is True)

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        name = call.spec.name
        self.lookups[name] += 1
        answer = obj(self._tool(name).get("lookup", {})).get(call.effect_key)
        if not isinstance(answer, dict):
            return LookupUnknown("no scripted answer")
        final = answer.get("final") is True
        if answer.get("result") == "found" and final:
            output = answer.get("output", self._tool(name).get("output"))
            assert isinstance(output, str)
            return Found(output)
        if answer.get("result") == "not_found":
            return NotFound() if final else NotFoundNonfinal()
        return LookupUnknown("not final")

    async def terminate(self, call: Invocation) -> Termination:
        return (
            "terminated" if self._tool(call.spec.name).get("process") == "terminated" else "unknown"
        )

    def provider_now(self) -> int | None:
        return self.clock.now

    def counters(self) -> dict[str, dict[str, int]]:
        return {
            "dispatches": dict(self.dispatches),
            "new_executions": dict(self.new_executions),
            "lookups": dict(self.lookups),
        }


def _subset(expected: JsonValue, actual: JsonValue) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in expected.items()
        )
    return expected == actual and type(expected) is type(actual)


def matches(matcher: dict[str, JsonValue], event: StoredEvent) -> bool:
    """An `EventMatcher` (spec/conformance/README.md, "Matching appended")."""
    wire = obj(to_json(event))
    for key in MATCHED_EXACTLY:
        if key in matcher and matcher[key] != wire[key]:
            return False
    if "actor_kind" in matcher and matcher["actor_kind"] != obj(wire["actor"])["kind"]:
        return False
    return _subset(matcher.get("data", {}), wire["data"])


def error_json(error: ParseError) -> Err[str]:
    return Err(json.dumps({"code": error.code, "seq": error.seq}))


async def import_and_read(case: Path, log: bytes, now: int) -> Ok[VerifiedLog] | Err[str]:
    """Imports an export into a fresh store holding the case's artifacts and reads the last
    branch back from SQLite."""
    verified = verify_export(log, now)
    if isinstance(verified, Err):
        return error_json(verified.error)
    opened = await SqliteStore.open(artifacts=stored_artifacts(case))
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        stored = await store.import_log(verified.value)
        if isinstance(stored, Err):
            return error_json(stored.error)
        branch = verified.value.segments[-1].header.branch_id
        if verified.value.head_verified:
            # SQLite and JSONL are one contract: the export is the imported bytes.
            assert await store.export(branch) == Ok(log)
        read = await store.read(branch, now)
    finally:
        await store.close()
    assert isinstance(read, Ok), read
    return read

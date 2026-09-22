"""The shared conformance corpus (spec/conformance/cases): reading case files, and the test-kit
sandbox the `recover` cases script. Runners import this; it holds no per-case code."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from threads.log import JsonObject, ToolSpec
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFound, NotFoundNonfinal
from threads.loop.tools import Dispatched, Invocation, Output, Termination, schema_error
from threads.store import MemoryArtifacts

CASES = Path(__file__).resolve().parents[2] / "spec" / "conformance" / "cases"
IMPL = "threads-py"
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
    dispatches: Counter[str] = field(default_factory=Counter[str])
    new_executions: Counter[str] = field(default_factory=Counter[str])
    lookups: Counter[str] = field(default_factory=Counter[str])

    def _tool(self, name: str) -> dict[str, JsonValue]:
        return obj(obj(self.script.get("tools", {})).get(name, {}))

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        return schema_error(spec, input)

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

"""The tool bodies of a rerun: every pinned tool answers from the case, never from a sandbox. A
saved case's read-only calls answer from sandbox.json v2 by (tool, args_hash, occurrence); the
corpus's scripted sandbox (the v1 shape) answers by tool name, with provider dedup, lookups and
process checks; mediated calls answer from the recorded stubs. A read-only call the recording
never made fails closed."""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.eval_v1 import RecallRecord, SandboxResult
from threads._json_schema import holds
from threads.evals.case_dir import CaseSandbox
from threads.log import JsonObject, ParseError, ToolSpec
from threads.loop.model import (
    Found,
    LookupResult,
    LookupUnknown,
    NotFound,
    NotFoundNonfinal,
)
from threads.loop.stubs import StubGateway
from threads.loop.tools import Dispatched, Invocation, Output, Reference, Termination
from threads.result import Err, Ok
from threads.thread.case_files import args_hash

_RECALLS: Final[Mapping[str, str]] = {"search_memory": "memory", "search_knowledge": "knowledge"}
"""The recalling tools: their recorded items come back as the call's references."""

type ReadArtifact = Callable[[str], Ok[bytes] | Err[ParseError]]


def _references(record: RecallRecord) -> tuple[Reference, ...]:
    out: list[Reference] = []
    for item in record.items:
        source = item.source
        if source not in ("memory", "knowledge") or not isinstance(item.text, str):
            continue
        origin = item.origin
        version = origin.version if isinstance(origin.version, str) else ""
        location = origin.location if isinstance(origin.location, str) else None
        out.append(Reference(source, origin.id, version, item.text, location))
    return tuple(out)


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


class Answers:
    """The rerun's tool runner (loop/tools.py `ToolRunner`)."""

    def __init__(
        self,
        sandbox: CaseSandbox | None,
        recall: Sequence[RecallRecord],
        stubs: StubGateway | None,
        read: ReadArtifact,
        now: Callable[[], int],
    ) -> None:
        self._results = () if sandbox is None else sandbox.results
        self._v1: Mapping[str, Mapping[str, JsonValue]] = (
            {} if sandbox is None or sandbox.v1 is None else sandbox.v1
        )
        self._recall = list(recall)
        self._stubs = stubs
        self._read = read
        self._now = now
        self._seen: Counter[tuple[str, str]] = Counter()
        self._used: set[int] = set()
        self._recalled = 0
        self.unrecorded: int = 0
        self.dispatches: Counter[str] = Counter()
        self.new_executions: Counter[str] = Counter()
        self.lookups: Counter[str] = Counter()

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        """The pinned schema, checked with the log's own keyword subset; a keyword it can't
        check is taken as recorded (the recording's tool already accepted the call)."""
        full = self._full_spec(spec)
        if full.input_schema is MISSING:
            return None
        try:
            return None if holds(dict(full.input_schema), dict(input)) else "invalid input"
        except TypeError:
            return None

    def _full_spec(self, spec: ToolSpec) -> ToolSpec:
        """A deferred tool's full spec, from the artifact its reference form names."""
        if spec.spec_ref is MISSING:
            return spec
        got = self._read(spec.spec_ref.sha256)
        if isinstance(got, Err):
            raise AssertionError(f"a pinned spec_ref reads back: {got.error.message}")
        return ToolSpec.model_validate_json(got.value)

    async def dispatch(self, call: Invocation) -> Dispatched:
        name = call.spec.name
        if name in self._v1:
            return self._scripted(name, call.effect_key)
        if call.spec.effect_class != "read_only" and self._stubs is not None:
            return await self._stubs.dispatch(call)
        return self._recorded(name, call.input)

    def _scripted(self, name: str, effect_key: str) -> Output:
        tool = self._v1[name]
        self.dispatches[name] += 1
        keys = tool.get("executed_keys")
        done = keys.get(effect_key) if isinstance(keys, dict) else None
        if isinstance(done, str):
            return Output(done)
        self.new_executions[name] += 1
        return Output(_text(tool.get("output")), tool.get("is_error") is True)

    def _recorded(self, name: str, input: JsonObject) -> Output:
        key = (name, args_hash(input))
        self._seen[key] += 1
        found = next(
            (
                (i, r)
                for i, r in enumerate(self._results)
                if (r.tool, r.args_hash, r.occurrence) == (*key, self._seen[key])
            ),
            None,
        )
        if found is None:
            self.unrecorded += 1
            return Output(
                f"unrecorded_call: the saved turn made no {name} call with these arguments", True
            )
        index, r = found
        self._used.add(index)
        content = () if r.content is MISSING else tuple(r.content)
        refs = self._recalled_by(name)
        return Output(self._full(r), r.is_error, content=content, references=refs)

    def _full(self, r: SandboxResult) -> str:
        if r.ref is MISSING:
            return r.preview
        got = self._read(r.ref.sha256)
        return got.value.decode("utf-8") if isinstance(got, Ok) else r.preview

    def _recalled_by(self, name: str) -> tuple[Reference, ...]:
        source = _RECALLS.get(name)
        if source is None:
            return ()
        index = next((i for i, r in enumerate(self._recall) if r.source == source), None)
        if index is None:
            return ()
        self._recalled += 1
        return _references(self._recall.pop(index))

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        name = call.spec.name
        tool = self._v1.get(name)
        if tool is None:
            return LookupUnknown(f"no recorded lookup for {name}")
        self.lookups[name] += 1
        answers = tool.get("lookup")
        answer = answers.get(call.effect_key) if isinstance(answers, dict) else None
        if not isinstance(answer, dict):
            return LookupUnknown("no scripted answer")
        final = answer.get("final") is True
        if answer.get("result") == "found" and final:
            return Found(_text(answer.get("output", tool.get("output"))))
        if answer.get("result") == "not_found":
            return NotFound() if final else NotFoundNonfinal()
        return LookupUnknown("not final")

    async def terminate(self, call: Invocation) -> Termination:
        tool = self._v1.get(call.spec.name, {})
        return "terminated" if tool.get("process") == "terminated" else "unknown"

    def provider_now(self) -> int | None:
        return self._now()

    def counters(self) -> dict[str, dict[str, int]]:
        return {
            "dispatches": dict(self.dispatches),
            "new_executions": dict(self.new_executions),
            "lookups": dict(self.lookups),
        }

    def left(self) -> int:
        """Recorded results and recall no call used."""
        return len(self._results) - len(self._used) + len(self._recall)

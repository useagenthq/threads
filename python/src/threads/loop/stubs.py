"""The stub gateway: in stub mode every mediated operation is answered from
recorded stubs, matched by `(tool, args_hash, occurrence)` and consumed in order. An invocation
with no unconsumed match fails closed: nothing goes live, ever."""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue, TypeAdapter

from threads.log import JsonObject, ToolSpec
from threads.log.digest import canonical_sha256
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.tools import (
    Dispatched,
    Invocation,
    NotSent,
    Output,
    Termination,
    Validate,
    unbound,
)
from threads.result import Ok

_STR: TypeAdapter[str] = TypeAdapter(str, config={"strict": True})
_INT: TypeAdapter[int] = TypeAdapter(int, config={"strict": True})


@dataclass(frozen=True, slots=True)
class Stub:
    tool: str
    args_hash: str
    occurrence: int
    output: str
    is_error: bool = False


def parse_stubs(script: Mapping[str, JsonValue]) -> tuple[Stub, ...]:
    """A `stubs.json` StubScript, parsed at this boundary."""
    stubs = script.get("stubs")
    if not isinstance(stubs, list):
        raise ValueError("a stub script needs a stubs list")
    out: list[Stub] = []
    for entry in stubs:
        if not isinstance(entry, dict):
            raise ValueError("a stub is an object")
        out.append(
            Stub(
                _STR.validate_python(entry.get("tool")),
                _STR.validate_python(entry.get("args_hash")),
                _INT.validate_python(entry.get("occurrence")),
                _STR.validate_python(entry.get("output")),
                entry.get("is_error") is True,
            )
        )
    return tuple(out)


class StubGateway:
    """A tool runner that never reaches a live provider."""

    def __init__(self, stubs: Sequence[Stub], validate: Validate = unbound) -> None:
        self._stubs = list(stubs)
        self._validate = validate
        self._seen: Counter[tuple[str, str]] = Counter()
        self.consumed = 0
        self.unmatched = 0

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        return self._validate(spec, input)

    async def dispatch(self, call: Invocation) -> Dispatched:
        hashed = canonical_sha256(dict(call.input))
        if not isinstance(hashed, Ok):
            raise AssertionError("a parsed call input always canonicalizes")
        key = (call.spec.name, hashed.value)
        occurrence = self._seen[key]
        self._seen[key] += 1
        for index, stub in enumerate(self._stubs):
            if (stub.tool, stub.args_hash, stub.occurrence) == (*key, occurrence):
                del self._stubs[index]
                self.consumed += 1
                return Output(stub.output, stub.is_error)
        self.unmatched += 1
        return NotSent(unmatched=True)

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return LookupUnknown(f"stub mode has no lookup for {call.spec.name}")

    async def terminate(self, call: Invocation) -> Termination:
        return "unknown"

    def provider_now(self) -> int | None:
        return None

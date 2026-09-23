"""read_tool_result: host-side and read_only. It reads a recorded result
back by call_id: the verified `ref` bytes of a spilled one, else its preview, so a truncated or
cleared result stays readable from any tool source, also after a fork."""

from collections.abc import Callable, Sequence

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import ReadToolResultInput
from threads.log import Event, JsonObject, ToolResultEvent, ToolSpec
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.tools import Dispatched, Invocation, Output, Termination
from threads.result import Err
from threads.store import SqliteStore
from threads.tools.runner import parse


class ReadResults:
    def __init__(self, store: SqliteStore, events: Callable[[], Sequence[Event]]) -> None:
        self._store = store
        self._events = events

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        parsed = parse(spec.name, input)
        return parsed.error if isinstance(parsed, Err) else None

    async def dispatch(self, call: Invocation) -> Dispatched:
        parsed = parse(call.spec.name, call.input)
        if isinstance(parsed, Err) or not isinstance(parsed.value, ReadToolResultInput):
            raise AssertionError("a dispatched call was parsed first")
        args = parsed.value
        recorded = next(
            (
                e
                for e in reversed(self._events())
                if isinstance(e, ToolResultEvent) and e.data.call_id == args.call_id
            ),
            None,
        )
        if recorded is None:
            return Output(f"not_found: no result for call {args.call_id}", True)
        data = recorded.data.preview.encode("utf-8")
        ref = recorded.data.ref
        if ref is not MISSING:
            got = await self._store.get_artifact(ref.sha256)
            if isinstance(got, Err):
                return Output(f"{got.error.code}: {got.error.message}", True)
            if len(got.value) != ref.bytes:
                return Output(f"artifact_corrupt: {ref.sha256} length", True)
            data = got.value
        end = min(len(data), args.offset + args.length)
        piece = data[min(args.offset, end) : end].decode("utf-8", "replace")
        return Output(f"[bytes {args.offset}-{end} of {len(data)}]\n{piece}")

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return LookupUnknown("read_tool_result has no lookup")

    async def terminate(self, call: Invocation) -> Termination:
        return "unknown"

    def provider_now(self) -> int | None:
        return None

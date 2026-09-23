"""The scripted model (test kit): plays a conformance `ModelScript` in order, never a network.

The script is parsed at this boundary: its parts and usage are the event schema's own types.
"""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Final

from pydantic import JsonValue, TypeAdapter

from threads.log import AdapterRef, ModelRef, OutputPart, TextPart, Usage
from threads.log import Model as ModelLimits
from threads.loop.model import (
    Delta,
    Done,
    Found,
    LookupResult,
    LookupUnknown,
    ModelChunk,
    ModelContext,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    NotFound,
    NotFoundNonfinal,
    PartChunk,
    Rejected,
    RejectReason,
    StopReason,
)
from threads.result import Err

_PARTS: TypeAdapter[list[OutputPart]] = TypeAdapter(list[OutputPart])
_STOP: TypeAdapter[StopReason] = TypeAdapter(StopReason)
_REASON: TypeAdapter[RejectReason] = TypeAdapter(RejectReason)
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_INT: TypeAdapter[int] = TypeAdapter(int, config={"strict": True})
_STR: TypeAdapter[str] = TypeAdapter(str, config={"strict": True})

SCRIPTED_INFO: Final = ModelInfo(
    model=ModelRef(provider="scripted", name="scripted-1"),
    adapter=AdapterRef(name="scripted", version="1", settings={}),
    params={"max_tokens": 1024},
    limits=ModelLimits(
        provider="scripted",
        name="scripted-1",
        context_window=200_000,
        max_output_tokens=8192,
        input_billing_bound="context_window",
    ),
    lookup="none",
)

type Entry = ModelResponse | Rejected


class ScriptExhaustedError(AssertionError):
    """A model call past the end of the script: the test's script is wrong, so this raises."""


class ScriptedModel:
    """spec/api.json `Model`, deterministic: each `send` plays the next entry."""

    def __init__(self, entries: Sequence[Entry], lookups: Mapping[str, JsonValue]) -> None:
        self._entries = list(entries)
        self._lookups = lookups
        self._info = SCRIPTED_INFO if not lookups else _with_lookup()
        self.sent: list[ModelRequest] = []
        self.looked_up: list[str] = []
        self.before_send: Callable[[ModelRequest], None] = lambda _request: None
        """A test hook run as each request reaches the model, before anything is played."""

    @property
    def info(self) -> ModelInfo:
        return self._info

    @property
    def remaining(self) -> int:
        """Entries not yet played. A finished test expects 0."""
        return len(self._entries)

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        if not self._entries:
            raise ScriptExhaustedError(f"no scripted response left for {request.request_id}")
        if isinstance(await context.fence(), Err):
            return
        self.before_send(request)
        self.sent.append(request)
        entry = self._entries.pop(0)
        if isinstance(entry, Rejected):
            yield entry
            return
        for part in entry.content:
            if isinstance(part, TextPart):
                yield Delta(part.text)
            yield PartChunk(part)
        yield Done(entry.stop_reason, entry.usage)

    async def lookup(self, request_id: str, context: ModelContext) -> LookupResult[ModelResponse]:
        if isinstance(await context.fence(), Err):
            return LookupUnknown("the lease moved before the lookup was sent")
        self.looked_up.append(request_id)
        # The script keys answers by the model_request event_id, the id's last component.
        answer = self._lookups.get(request_id.rsplit(":", 1)[-1])
        if not isinstance(answer, dict):
            return LookupUnknown("no scripted answer")
        final = answer.get("final") is True
        match answer.get("result"):
            case "found" if final:
                found = _response(_OBJECT.validate_python(answer.get("response")))
                if isinstance(found, Rejected):
                    raise ValueError("a lookup answer must be a response")
                provider_id = answer.get("provider_request_id")
                ident = None if provider_id is None else _STR.validate_python(provider_id)
                return Found(replace(found, provider_request_id=ident))
            case "not_found" if final:
                return NotFound()
            case "not_found":
                return NotFoundNonfinal()
            case _:
                return LookupUnknown("the answer is not final")


def _with_lookup() -> ModelInfo:
    base = SCRIPTED_INFO
    return ModelInfo(base.model, base.adapter, base.params, base.limits, "final")


def _response(entry: dict[str, JsonValue]) -> Entry:
    error = entry.get("error")
    if error is not None:
        e = _OBJECT.validate_python(error)
        retry = e.get("retry_after_ms")
        return Rejected(
            _REASON.validate_python(e.get("reason")),
            _INT.validate_python(e.get("http_status")),
            None if retry is None else _INT.validate_python(retry),
        )
    return ModelResponse(
        _PARTS.validate_python(entry.get("content")),
        _STOP.validate_python(entry.get("stop_reason")),
        Usage.model_validate(entry.get("usage")),
        None,
    )


def scripted_model(script: Mapping[str, JsonValue]) -> ScriptedModel:
    """spec/api.json `scriptedModel`: consumes `script.responses` in order; `script.lookup`
    answers response recovery by model_request event_id. Raises on a malformed script."""
    responses = script.get("responses")
    if not isinstance(responses, list):
        raise ValueError("a model script needs a responses list")
    lookups = script.get("lookup", {})
    if not isinstance(lookups, dict):
        raise ValueError("a model script's lookup is an object")
    entries = [_response(_OBJECT.validate_python(r)) for r in responses]
    return ScriptedModel(entries, lookups)

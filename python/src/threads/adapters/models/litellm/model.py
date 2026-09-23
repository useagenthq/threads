"""`litellm()`: the LiteLLM bridge for providers without a first-party adapter.

One transport attempt per send: LiteLLM's retries are off (`num_retries=0`, `max_retries=0`),
and threads records and schedules every retry. The fence runs immediately
before LiteLLM is called.
"""

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from typing import TypeGuard, Unpack

import litellm as bridge
import openai as sdk
from litellm.exceptions import ContextWindowExceededError

from threads.adapters.models import transport
from threads.adapters.models.litellm.request import build
from threads.adapters.models.litellm.stream import Assembler
from threads.adapters.models.options import ModelOptions, info
from threads.adapters.models.render import check_adapter
from threads.adapters.models.render import parse as parse_render
from threads.log import AdapterRef, ModelRef
from threads.loop.model import (
    LookupResult,
    LookupUnknown,
    ModelChunk,
    ModelContext,
    ModelInfo,
    ModelRequest,
    ModelResponse,
)
from threads.result import Err

ADAPTER = "litellm"
VERSION = "1"
PROVIDER = "litellm"

type Complete = Callable[..., Awaitable[object]]
"""`litellm.acompletion`, or a stand-in with its signature."""

# LiteLLM's own annotations are partial: its entry point is typed here, once, as what the
# adapter relies on. Everything it returns is parsed before use.
ACOMPLETION: Complete = getattr(bridge, "acompletion")  # noqa: B009


class LiteLLMModel:
    """spec/api.json `Model` over LiteLLM. No response lookup.

    ponytail: the fence runs before `acompletion`, not at LiteLLM's socket write: LiteLLM uses
    a different HTTP stack per provider, so a lease lost during its own request preparation
    (for example a credential refresh) is not caught. Use a first-party adapter where that
    window matters.
    """

    def __init__(
        self, info: ModelInfo, complete: Complete, connection: Mapping[str, str] | None = None
    ) -> None:
        self._info = info
        self._complete = complete
        self._connection = dict(connection or {})
        """Credentials and endpoint: passed per call, never pinned in line 0 or logged."""

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        rendered = parse_render(request.body)
        check_adapter(rendered.head, ADAPTER)
        body = await build(rendered, context)
        if isinstance(await context.fence(), Err):
            return
        assembler = Assembler()
        started = False
        try:
            stream = await self._complete(**body, **self._connection, num_retries=0, max_retries=0)
            if not _is_stream(stream):
                raise TypeError("LiteLLM returned no stream")
            async for raw in stream:
                for chunk in assembler.feed(raw):
                    started = True
                    yield chunk
        except sdk.APIStatusError as error:
            if started:
                raise
            too_long = isinstance(error, ContextWindowExceededError)
            yield transport.rejection(error.status_code, error.response.headers, too_long)
            return
        for chunk in assembler.finish():
            yield chunk

    async def lookup(self, request_id: str, context: ModelContext) -> LookupResult[ModelResponse]:
        return LookupUnknown("LiteLLM has no lookup by client request id")


def _is_stream(value: object) -> TypeGuard[AsyncIterable[object]]:
    return isinstance(value, AsyncIterable)


def litellm(name: str, **options: Unpack[ModelOptions]) -> LiteLLMModel:
    """spec/api.json `litellm`. `name` is the LiteLLM route (`provider/model`); `params` are
    completion fields; `max_tokens` defaults to `max_output_tokens`. Credentials come from
    `api_key` or the provider's environment variable, as LiteLLM reads them."""
    declared = info(
        ModelRef(provider=PROVIDER, name=name),
        AdapterRef(name=ADAPTER, version=VERSION, settings={}),
        options,
        {"max_tokens": options["max_output_tokens"]},
        ("text", "image_ref", "document_ref"),
    )
    connection: dict[str, str] = {}
    if "api_key" in options:
        connection["api_key"] = options["api_key"]
    if "base_url" in options:
        connection["api_base"] = options["base_url"]
    return LiteLLMModel(declared, ACOMPLETION, connection)

"""`litellm()`: the LiteLLM bridge for providers without a first-party adapter.

One transport attempt per send: LiteLLM's retries are off (`num_retries=0`, `max_retries=0`),
and threads records and schedules every retry.

Fencing. Only the `openai/` route is supported: LiteLLM sends it through an OpenAI SDK client
the adapter supplies, whose transport awaits the fence at the real send. Every other route
uses LiteLLM's own HTTP stack, which prepares the request in a thread executor before sending,
so the fence can't sit at the real send. A send can carry provider-hosted tools, so those
routes are refused at setup (`transport_fence_unsupported`), never run with a weaker fence.
"""

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from functools import partial
from typing import Protocol, TypeGuard, Unpack

import litellm as bridge
import openai as sdk
from litellm.exceptions import ContextWindowExceededError

from threads.adapters.models import transport
from threads.adapters.models.litellm.request import build
from threads.adapters.models.litellm.stream import Assembler
from threads.adapters.models.options import ModelOptions, info
from threads.adapters.models.render import prepare
from threads.agents.config import ConfigError
from threads.log import AdapterRef, ModelRef
from threads.loop.model import (
    LookupResult,
    LookupUnknown,
    ModelChunk,
    ModelContext,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    Rejected,
)

ADAPTER = "litellm"
VERSION = "1"
PROVIDER = "litellm"

type Complete = Callable[..., Awaitable[object]]
"""`litellm.acompletion`, or a stand-in with its signature."""

# LiteLLM's own annotations are partial: its entry point is typed here, once, as what the
# adapter relies on. Everything it returns is parsed before use.
ACOMPLETION: Complete = getattr(bridge, "acompletion")  # noqa: B009


class LiteLLMModel:
    """spec/api.json `Model` over LiteLLM's `openai/` route. No response lookup.

    ponytail: one route. Add another when its HTTP client can be supplied and fenced.
    """

    def __init__(self, info: ModelInfo, complete: Complete) -> None:
        self._info = info
        self._complete = complete

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        prepared = await prepare(request.body, ADAPTER, context, build)
        if isinstance(prepared, Rejected):
            yield prepared
            return
        _, body = prepared
        try:
            with transport.attempt(context):
                stream = await self._complete(**body, num_retries=0, max_retries=0)
        except sdk.APIError as error:
            if transport.stale(error):
                yield Rejected("stale_epoch")
                return
            if not isinstance(error, sdk.APIStatusError):
                raise
            too_long = isinstance(error, ContextWindowExceededError)
            yield transport.rejection(error.status_code, _headers(error), too_long)
            return
        if not _is_stream(stream):
            raise TypeError("LiteLLM returned no stream")
        # The provider answered 200: a later failure is uncertainty to raise, even the status
        # error LiteLLM makes up for it (MidStreamFallbackError, 500). The stream is closed
        # here, before send ends: left open, the event loop's shutdown closes its HTTP body
        # generator while it runs ("aclose(): asynchronous generator is already running").
        assembler = Assembler()
        try:
            async for raw in stream:
                for chunk in assembler.feed(raw):
                    yield chunk
        finally:
            await stream.aclose()
        for chunk in assembler.finish():
            yield chunk

    async def lookup(self, request_id: str, context: ModelContext) -> LookupResult[ModelResponse]:
        return LookupUnknown("LiteLLM has no lookup by client request id")


def _headers(error: sdk.APIStatusError) -> Mapping[str, str]:
    """LiteLLM rebuilds the status error without the provider's headers and keeps them in
    `litellm_response_headers`: retry-after is read there, else from the response."""
    kept: object = getattr(error, "litellm_response_headers", None)
    return kept if _is_headers(kept) else error.response.headers


def _is_headers(value: object) -> TypeGuard[Mapping[str, str]]:
    return isinstance(value, Mapping)


class _Stream(AsyncIterable[object], Protocol):
    """LiteLLM's stream wrapper: iterated, then closed (it closes the HTTP response)."""

    async def aclose(self) -> None: ...


def _is_stream(value: object) -> TypeGuard[_Stream]:
    return hasattr(value, "aclose") and isinstance(value, AsyncIterable)


def litellm(name: str, **options: Unpack[ModelOptions]) -> LiteLLMModel:
    """A model behind LiteLLM's `openai/` route (any OpenAI-compatible endpoint via `base_url`).
    `params` are completion fields; `max_tokens` defaults to `max_output_tokens`. Credentials
    come from `api_key` or `OPENAI_API_KEY`. Other routes raise ConfigError
    `transport_fence_unsupported` (module docstring)."""
    if not name.startswith("openai/"):
        raise ConfigError(
            "transport_fence_unsupported",
            f"{name}: only LiteLLM's openai/ route sends through a transport threads can fence",
        )
    declared = info(
        ModelRef(provider=PROVIDER, name=name),
        AdapterRef(name=ADAPTER, version=VERSION, settings={}),
        options,
        {"max_tokens": options["max_output_tokens"]},
    )
    # LiteLLM sends this route through the OpenAI SDK client it is given: ours, fenced.
    sdk_client = sdk.AsyncOpenAI(
        api_key=options.get("api_key"),
        base_url=options.get("base_url"),
        max_retries=0,
        http_client=transport.client(),
    )
    return LiteLLMModel(declared, partial(ACOMPLETION, client=sdk_client))

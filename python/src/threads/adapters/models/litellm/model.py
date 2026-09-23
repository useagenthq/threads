"""`litellm()`: the LiteLLM bridge for providers without a first-party adapter.

One transport attempt per send: LiteLLM's retries are off (`num_retries=0`, `max_retries=0`),
and threads records and schedules every retry.

Fencing (ModelInfo.fence_point). The `openai/` route goes through an OpenAI SDK client the
adapter supplies, sending through the fenced transport: fenced at the real send
(`transport`). Every other route uses LiteLLM's own per-provider HTTP stack, so it is fenced
just before `acompletion` (`pre_call`). LiteLLM does queue there: `acompletion` prepares the
request in the event loop's default thread executor before sending, so a lease lost while
that executor is busy can still send once. That is cost-only: the stale owner can never
append the response, and effects never go through a model send.
"""

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from functools import partial
from typing import TypeGuard, Unpack

import litellm as bridge
import openai as sdk
from litellm.exceptions import ContextWindowExceededError

from threads.adapters.models import transport
from threads.adapters.models.litellm.request import build
from threads.adapters.models.litellm.stream import Assembler
from threads.adapters.models.options import ModelOptions, info
from threads.adapters.models.render import prepare
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

    ponytail: only the `openai/` route is fenced at the transport; the rest fence pre-call
    (module docstring). Supply fenced clients for more routes when their window matters.
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
        prepared = await prepare(request.body, ADAPTER, context, build)
        if isinstance(prepared, Rejected):
            yield prepared
            return
        _, body = prepared
        if self._info.fence_point == "pre_call" and isinstance(await context.fence(), Err):
            yield Rejected("stale_epoch")
            return
        assembler = Assembler()
        started = False
        try:
            with transport.attempt(context):
                stream = await self._complete(
                    **body, **self._connection, num_retries=0, max_retries=0
                )
            if not _is_stream(stream):
                raise TypeError("LiteLLM returned no stream")
            async for raw in stream:
                for chunk in assembler.feed(raw):
                    started = True
                    yield chunk
        except sdk.APIError as error:
            if transport.stale(error):
                yield Rejected("stale_epoch")
                return
            if started or not isinstance(error, sdk.APIStatusError):
                raise
            too_long = isinstance(error, ContextWindowExceededError)
            yield transport.rejection(error.status_code, _headers(error), too_long)
            return
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


def _is_stream(value: object) -> TypeGuard[AsyncIterable[object]]:
    return isinstance(value, AsyncIterable)


def litellm(name: str, **options: Unpack[ModelOptions]) -> LiteLLMModel:
    """A model behind LiteLLM. `name` is the LiteLLM route (`provider/model`); `params` are
    completion fields; `max_tokens` defaults to `max_output_tokens`. Credentials come from
    `api_key` or the provider's environment variable, as LiteLLM reads them."""
    openai_route = name.startswith("openai/")
    declared = info(
        ModelRef(provider=PROVIDER, name=name),
        AdapterRef(name=ADAPTER, version=VERSION, settings={}),
        options,
        {"max_tokens": options["max_output_tokens"]},
        "transport" if openai_route else "pre_call",
    )
    if openai_route:
        # LiteLLM sends this route through the OpenAI SDK client it is given: ours, fenced.
        sdk_client = sdk.AsyncOpenAI(
            api_key=options.get("api_key"),
            base_url=options.get("base_url"),
            max_retries=0,
            http_client=transport.client(),
        )
        return LiteLLMModel(declared, partial(ACOMPLETION, client=sdk_client))
    connection: dict[str, str] = {}
    if "api_key" in options:
        connection["api_key"] = options["api_key"]
    if "base_url" in options:
        connection["api_base"] = options["base_url"]
    return LiteLLMModel(declared, ACOMPLETION, connection)

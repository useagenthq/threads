"""`openai()`: the Responses API adapter over the official `openai` SDK's async client.

One transport attempt per send: SDK retries are off, and threads records and schedules every
retry. The SDK sends through a fenced HTTP client, so a writer that lost its
lease while the SDK prepared or queued the request sends nothing.
"""

from collections.abc import AsyncIterator, Mapping
from typing import Unpack

import httpx2
import openai as sdk

from threads.adapters.models import transport
from threads.adapters.models.openai.request import PROVIDER, build
from threads.adapters.models.openai.stream import Assembler, ProviderStreamError
from threads.adapters.models.openai.wire import parse
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

ADAPTER = "openai"
VERSION = "1"
_STREAM_ERRORS: Mapping[str, Rejected] = {
    "rate_limit_exceeded": Rejected("rate_limited"),
    "server_error": Rejected("server_error"),
}
_TOO_LONG = "context_length_exceeded"


class OpenAIModel:
    """spec/api.json `Model` for the OpenAI Responses API. No response lookup: a stateless
    request can't be found by client request id, so recovery re-sends under its budget."""

    def __init__(self, info: ModelInfo, client: sdk.AsyncOpenAI) -> None:
        self._info = info
        self._client = client

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        prepared = await prepare(request.body, ADAPTER, context, build)
        if isinstance(prepared, Rejected):
            yield prepared
            return
        rendered, body = prepared
        try:
            with transport.attempt(context):
                response = await self._client.post(
                    "/responses",
                    body=body,
                    cast_to=httpx2.Response,
                    stream=True,
                    stream_cls=sdk.AsyncStream[object],
                )
        except sdk.APIStatusError as error:
            yield transport.rejection(error.status_code, error.response.headers, _too_long(error))
            return
        except sdk.APIConnectionError as error:
            if transport.stale(error):
                return
            if transport.not_sent(error):
                yield Rejected("server_error")
                return
            raise
        assembler = Assembler(context, rendered.head.model.name)
        async for chunk in transport.relay(response, parse, assembler.feed, _rejected):
            yield chunk

    async def lookup(self, request_id: str, context: ModelContext) -> LookupResult[ModelResponse]:
        return LookupUnknown("a stateless Responses API request has no lookup by client id")


def _rejected(error: Exception) -> Rejected | None:
    """A failed response before any content is the provider refusing it."""
    if isinstance(error, ProviderStreamError):
        return _STREAM_ERRORS.get(error.code or "", Rejected("provider_error"))
    return None


def _too_long(error: sdk.APIStatusError) -> bool:
    return error.code == _TOO_LONG


def openai(name: str, **options: Unpack[ModelOptions]) -> OpenAIModel:
    """An OpenAI model (the `openai()` of spec/api.json conventions.adapters).
    `max_output_tokens` is pinned as the request's cap; other `params` are Responses API
    fields. `api_key` falls back to OPENAI_API_KEY."""
    declared = info(
        ModelRef(provider=PROVIDER, name=name),
        AdapterRef(name=ADAPTER, version=VERSION, settings={}),
        options,
        {"max_output_tokens": options["max_output_tokens"]},
        ("text", "image_ref", "document_ref"),
    )
    return OpenAIModel(declared, client(options.get("api_key"), options.get("base_url")))


def client(
    api_key: str | None = None,
    base_url: str | None = None,
    http: httpx2.AsyncBaseTransport | None = None,
) -> sdk.AsyncOpenAI:
    """The SDK client: retries off, sending through the fenced transport (`http` for tests)."""
    return sdk.AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        http_client=transport.client(http),
    )

"""`anthropic()`: the Messages API adapter over the official `anthropic` SDK's async client.

One transport attempt per send: SDK retries are off, and threads records and schedules every
retry. The SDK sends through a fenced HTTP client, so a writer that lost its
lease while the SDK prepared or queued the request sends nothing.
"""

from collections.abc import AsyncIterator, Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING, Unpack

import anthropic as sdk
import httpx2

from threads.adapters.models import transport
from threads.adapters.models.anthropic.request import PROVIDER, build
from threads.adapters.models.anthropic.stream import Assembler, ProviderStreamError
from threads.adapters.models.anthropic.wire import parse
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
    Rejected,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

ADAPTER = "anthropic"
VERSION = "1"
_STREAM_ERRORS: Mapping[str, Rejected] = {
    "overloaded_error": Rejected("overloaded"),
    "rate_limit_error": Rejected("rate_limited"),
    "api_error": Rejected("server_error"),
}


class AnthropicModel:
    """spec/api.json `Model` for the Anthropic Messages API. No response lookup: the API has no
    way to find a response by client request id, so recovery re-sends under its budget."""

    def __init__(self, info: ModelInfo, client: sdk.AsyncAnthropic) -> None:
        self._info = info
        self._client = client

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        rendered = parse_render(request.body)
        check_adapter(rendered.head, ADAPTER)
        body = await build(rendered, context)
        try:
            with transport.attempt(context):
                response = await self._client.post(
                    "/v1/messages",
                    body=body.json,
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
        assembler = Assembler(context, rendered.head.model.name, body.documents)
        async for chunk in transport.relay(response, parse, assembler.feed, _rejected):
            yield chunk

    async def lookup(self, request_id: str, context: ModelContext) -> LookupResult[ModelResponse]:
        return LookupUnknown("the Messages API has no lookup by client request id")


def _rejected(error: Exception) -> Rejected | None:
    return _STREAM_ERRORS.get(error.kind) if isinstance(error, ProviderStreamError) else None


def _too_long(error: sdk.APIStatusError) -> bool:
    return error.status_code == HTTPStatus.BAD_REQUEST and "prompt is too long" in error.message


class AnthropicOptions(ModelOptions, total=False):
    citations: bool
    """Enables citations on documents (an adapter setting, so pinned in line 0)."""


def anthropic(name: str, **options: Unpack[AnthropicOptions]) -> AnthropicModel:
    """An Anthropic model (the `anthropic()` of spec/api.json conventions.adapters).
    `max_tokens` defaults to `max_output_tokens`; other `params` are Messages API fields.
    `api_key` falls back to ANTHROPIC_API_KEY."""
    settings: dict[str, JsonValue] = {"citations": True} if options.get("citations") else {}
    declared = info(
        ModelRef(provider=PROVIDER, name=name),
        AdapterRef(name=ADAPTER, version=VERSION, settings=settings),
        options,
        {"max_tokens": options["max_output_tokens"]},
        ("text", "image_ref", "document_ref"),
    )
    return AnthropicModel(declared, client(options.get("api_key"), options.get("base_url")))


def client(
    api_key: str | None = None,
    base_url: str | None = None,
    http: httpx2.AsyncBaseTransport | None = None,
) -> sdk.AsyncAnthropic:
    """The SDK client: retries off, sending through the fenced transport (`http` for tests)."""
    return sdk.AsyncAnthropic(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        http_client=transport.client(http),
    )

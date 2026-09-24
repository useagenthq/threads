"""`anthropic()`: the Messages API adapter over the official `anthropic` SDK's async client.

One transport attempt per send: SDK retries are off, and threads records and schedules every
retry. The SDK sends through a fenced HTTP client, so a writer that lost its
lease while the SDK prepared or queued the request sends nothing.
"""

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from http import HTTPStatus
from typing import Unpack

import anthropic as sdk
import httpx2

from threads.adapters.models import transport
from threads.adapters.models.anthropic.request import PROVIDER, build
from threads.adapters.models.anthropic.stream import Assembler, ProviderStreamError
from threads.adapters.models.anthropic.wire import parse
from threads.adapters.models.hosted import HostedTool, declare
from threads.adapters.models.options import ModelOptions, info
from threads.adapters.models.render import prepare
from threads.log import AdapterRef, ModelRef
from threads.loop.model import (
    ModelChunk,
    ModelContext,
    ModelInfo,
    ModelRequest,
    Rejected,
)
from threads.secrets import Secret, credential

ADAPTER = "anthropic"
API_KEY = "ANTHROPIC_API_KEY"
VERSION = "1"
_STREAM_ERRORS: Mapping[str, Rejected] = {
    "overloaded_error": Rejected("overloaded"),
    "rate_limit_error": Rejected("rate_limited"),
    "api_error": Rejected("server_error"),
}


class AnthropicModel:
    """spec/api.json `Model` for the Anthropic Messages API. No response lookup: the API has no
    way to find a response by client request id, so recovery re-sends under its budget."""

    def __init__(
        self,
        info: ModelInfo,
        api_key: str | Secret | None = None,
        base_url: str | None = None,
        http: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._info = info
        self._key = credential(ADAPTER, "api_key", api_key, API_KEY)
        self._base_url, self._http = base_url, http
        self._client: sdk.AsyncAnthropic | None = None

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def setup(self) -> None:
        """Resolves the key on the host. The SDK client is made by the first send, on the
        run's own event loop."""
        self._key()

    def _sdk(self) -> sdk.AsyncAnthropic:
        if self._client is None:
            self._client = client(self._key(), self._base_url, self._http)
        return self._client

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        prepared = await prepare(request.body, ADAPTER, context, build)
        if isinstance(prepared, Rejected):
            yield prepared
            return
        rendered, body = prepared
        try:
            with transport.attempt(context):
                response = await self._sdk().post(
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
                yield Rejected("stale_epoch")
                return
            if transport.not_sent(error):
                yield Rejected("server_error")
                return
            raise
        assembler = Assembler(context, rendered.head.model.name, body.documents)
        async for chunk in transport.relay(response, parse, assembler.feed, _rejected):
            yield chunk


def _rejected(error: Exception) -> Rejected | None:
    return _STREAM_ERRORS.get(error.kind) if isinstance(error, ProviderStreamError) else None


def _too_long(error: sdk.APIStatusError) -> bool:
    return error.status_code == HTTPStatus.BAD_REQUEST and "prompt is too long" in error.message


class AnthropicOptions(ModelOptions, total=False):
    citations: bool
    """Enables citations on documents (an adapter setting, so pinned in line 0)."""
    hosted_tools: Sequence[HostedTool]
    """Server tools, sent as given and pinned in line 0: web search and web fetch only
    (`web_search_YYYYMMDD`, `web_fetch_YYYYMMDD`); anything else raises hosted_tool_unsupported."""


def _web(kind: str) -> bool:
    return re.fullmatch(r"(web_search|web_fetch)_\d{8}", kind) is not None


def anthropic(name: str, **options: Unpack[AnthropicOptions]) -> AnthropicModel:
    """An Anthropic model (the `anthropic()` of spec/api.json conventions.adapters).
    `max_tokens` defaults to `max_output_tokens`; other `params` are Messages API fields.
    `api_key` defaults to `secret("ANTHROPIC_API_KEY")`, resolved at setup."""
    settings, hosted = declare(options.get("hosted_tools", ()), _web, "name")
    if options.get("citations"):
        settings["citations"] = True
    declared = info(
        ModelRef(provider=PROVIDER, name=name),
        AdapterRef(name=ADAPTER, version=VERSION, settings=settings),
        options,
        {"max_tokens": options["max_output_tokens"]},
        hosted,
    )
    return AnthropicModel(declared, options.get("api_key"), options.get("base_url"))


def client(
    api_key: str,
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

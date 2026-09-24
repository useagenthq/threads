"""`openai()`: the Responses API adapter over the official `openai` SDK's async client.

One transport attempt per send: SDK retries are off, and threads records and schedules every
retry. The SDK sends through a fenced HTTP client, so a writer that lost its
lease while the SDK prepared or queued the request sends nothing.
"""

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Final, Unpack

import httpx2
import openai as sdk
from pydantic import JsonValue

from threads.adapters.loop_resources import LoopResources
from threads.adapters.models import transport
from threads.adapters.models.hosted import declare
from threads.adapters.models.openai.request import PROVIDER, build
from threads.adapters.models.openai.stream import Assembler, ProviderStreamError
from threads.adapters.models.openai.wire import parse
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

ADAPTER = "openai"
API_KEY = "OPENAI_API_KEY"
VERSION = "1"
_STREAM_ERRORS: Mapping[str, Rejected] = {
    "rate_limit_exceeded": Rejected("rate_limited"),
    "server_error": Rejected("server_error"),
}
_TOO_LONG = "context_length_exceeded"


class OpenAIModel:
    """spec/api.json `Model` for the OpenAI Responses API. No response lookup: a stateless
    request can't be found by client request id, so recovery re-sends under its budget."""

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
        self._clients: LoopResources[sdk.AsyncOpenAI] = LoopResources(ADAPTER, _close)

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def setup(self) -> None:
        """Resolves the key on the host. The SDK client is made by the first send, on the
        run's own event loop, and closed when nothing holds that loop any more."""
        self._key()

    def _sdk(self) -> sdk.AsyncOpenAI:
        return self._clients.get(lambda: client(self._key(), self._base_url, self._http))

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        prepared = await prepare(request.body, ADAPTER, context, build)
        if isinstance(prepared, Rejected):
            yield prepared
            return
        rendered, body = prepared
        try:
            with transport.attempt(context):
                response = await self._sdk().post(
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
                yield Rejected("stale_epoch")
                return
            if transport.not_sent(error):
                yield Rejected("server_error")
                return
            raise
        assembler = Assembler(context, rendered.head.model.name)
        async for chunk in transport.relay(response, parse, assembler.feed, _rejected):
            yield chunk


def _rejected(error: Exception) -> Rejected | None:
    """A failed response before any content is the provider refusing it."""
    if isinstance(error, ProviderStreamError):
        return _STREAM_ERRORS.get(error.code or "", Rejected("provider_error"))
    return None


def _too_long(error: sdk.APIStatusError) -> bool:
    return error.code == _TOO_LONG


class OpenAIOptions(ModelOptions, total=False):
    """Limits default from spec/models/openai.v1.json when the model id is listed there."""

    hosted_tools: Sequence[Mapping[str, JsonValue]]
    """Hosted tools, sent as given and pinned in line 0: web search only (`web_search`, with an
    optional `_preview` and date); anything else raises hosted_tool_unsupported."""


RESERVED: Final = (
    *("model", "input", "instructions", "tools", "stream", "store", "include"),
    *("previous_response_id", "conversation", "background", "max_tokens", "max_output_tokens"),
)
"""Request fields the adapter derives from the render, and both cap keys (the max_tokens
option)."""


def _web(kind: str) -> bool:
    return re.fullmatch(r"web_search(_preview)?(_\d{4}_\d{2}_\d{2})?", kind) is not None


def openai(model: str, **options: Unpack[OpenAIOptions]) -> OpenAIModel:
    """An OpenAI model by its exact id: `openai("gpt-5.5")` (spec/api.json conventions.adapters).
    The cap is pinned as `params.max_tokens` (default min(8192, max_output_tokens)) and sent as
    `max_output_tokens`; other `params` are Responses API fields. `api_key` defaults to
    `secret("OPENAI_API_KEY")`, resolved at setup."""
    settings, hosted = declare(options.get("hosted_tools", ()), _web, "type")
    declared = info(
        ModelRef(provider=PROVIDER, name=model),
        AdapterRef(name=ADAPTER, version=VERSION, settings=settings),
        options,
        RESERVED,
        hosted,
    )
    return OpenAIModel(declared, options.get("api_key"), options.get("base_url"))


def client(
    api_key: str,
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


async def _close(client: sdk.AsyncOpenAI) -> None:
    await client.close()

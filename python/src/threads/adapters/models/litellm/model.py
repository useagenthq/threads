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
from dataclasses import dataclass, replace
from functools import partial
from typing import Final, Literal, Protocol, TypeGuard, Unpack

import litellm as bridge
import openai as sdk
from litellm.exceptions import ContextWindowExceededError

from threads.adapters.loop_resources import LoopResources
from threads.adapters.models import transport
from threads.adapters.models.litellm.request import build
from threads.adapters.models.litellm.stream import Assembler
from threads.adapters.models.options import ExplicitOptions, info
from threads.adapters.models.render import prepare
from threads.agents.config import ConfigError
from threads.log import AdapterRef, ModelRef
from threads.loop.model import (
    Cache,
    ModelChunk,
    ModelContext,
    ModelInfo,
    ModelRequest,
    Rejected,
)
from threads.secrets import Secret, credential

ADAPTER = "litellm"
API_KEY = "OPENAI_API_KEY"
VERSION = "1"
PROVIDER = "litellm"

type Complete = Callable[..., Awaitable[object]]
"""`litellm.acompletion`, or a stand-in with its signature."""

# LiteLLM's own annotations are partial: its entry point is typed here, once, as what the
# adapter relies on. Everything it returns is parsed before use.
ACOMPLETION: Complete = getattr(bridge, "acompletion")  # noqa: B009


async def _nothing() -> None:
    pass


@dataclass(frozen=True, slots=True)
class Connection:
    """`acompletion` bound to a client for one event loop, and how to close that client."""

    complete: Complete
    close: Callable[[], Awaitable[None]] = _nothing


class LiteLLMModel:
    """spec/api.json `Model` over LiteLLM's `openai/` route. No response lookup.

    ponytail: one route. Add another when its HTTP client can be supplied and fenced.
    """

    def __init__(
        self, info: ModelInfo, api_key: str | Secret | None, connect: Callable[[str], Connection]
    ) -> None:
        self._info = info
        self._key = credential(ADAPTER, "api_key", api_key, API_KEY)
        self._connect = connect
        """Makes `acompletion` bound to a client for the resolved key, on the first send."""
        self._connections: LoopResources[Connection] = LoopResources(ADAPTER, _disconnect)

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def setup(self) -> None:
        """Resolves the key on the host. The client is made by the first send, on the run's
        own event loop, and closed when nothing holds that loop any more."""
        self._key()

    def _completion(self) -> Complete:
        return self._connections.get(lambda: self._connect(self._key())).complete

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        prepared = await prepare(request.body, ADAPTER, context, build)
        if isinstance(prepared, Rejected):
            yield prepared
            return
        _, body = prepared
        try:
            with transport.attempt(context):
                stream = await self._completion()(**body, num_retries=0, max_retries=0)
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
        except BaseException as outcome:
            await _close(stream, outcome)
            raise
        await _close(stream, None)
        for chunk in assembler.finish():
            yield chunk


async def _close(stream: "_Stream", outcome: BaseException | None) -> None:
    """Closes the stream without letting a close failure replace what the stream decided: an
    error keeps its own type (the failure is noted on it), and a completed stream stays done."""
    try:
        await stream.aclose()
    except Exception as failure:
        if outcome is not None:
            outcome.add_note(f"closing the stream also failed: {failure!r}")


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


RESERVED: Final = ("model", "messages", "tools", "stream", "stream_options", "max_tokens")
"""Request fields the adapter derives from the render, and the cap (the max_tokens option)."""


class LiteLLMOptions(ExplicitOptions, total=False):
    cache_ttl_ms: int | Literal["none"]
    """How long the provider keeps prompt-cache entries, in ms, or "none" when it doesn't cache.
    threads can't see the provider behind `base_url`, so an agent using this model needs it or
    context.cache_ttl_ms."""


def _cache(given: object) -> Cache | None:
    """The declared cache lifetime; None is unknown. An untyped caller may pass anything."""
    if given is None:
        return None
    if given == "none":
        return "none"
    # Exactly int: a bool is an int to Python.
    if type(given) is int and given > 0:
        return {"ttl_ms": given}
    raise ConfigError(
        "invalid_config",
        f'litellm cache_ttl_ms must be a positive whole number of milliseconds or "none", '
        f"not {given!r}",
    )


def litellm(name: str, **options: Unpack[LiteLLMOptions]) -> LiteLLMModel:
    """A model behind LiteLLM's `openai/` route (any OpenAI-compatible endpoint via `base_url`).
    Both limits are required: the name doesn't identify the model behind `base_url`. `params`
    are completion fields; `max_tokens` defaults to min(8192, max_output_tokens). `api_key`
    defaults to `secret("OPENAI_API_KEY")`, resolved at setup. Any other route is refused at
    setup with `transport_fence_unsupported` (module docstring)."""
    if not name.startswith("openai/"):
        raise ConfigError(
            "transport_fence_unsupported",
            f"{name}: only LiteLLM's openai/ route sends through a transport threads can fence",
        )
    declared = info(
        ModelRef(provider=PROVIDER, name=name),
        AdapterRef(name=ADAPTER, version=VERSION, settings={}),
        options,
        RESERVED,
    )
    declared = replace(declared, cache=_cache(options.get("cache_ttl_ms")))
    base_url = options.get("base_url")

    def connect(key: str) -> Connection:
        # LiteLLM sends this route through the OpenAI SDK client it is given: ours, fenced.
        client = sdk.AsyncOpenAI(
            api_key=key, base_url=base_url, max_retries=0, http_client=transport.client()
        )
        return Connection(partial(ACOMPLETION, client=client), client.close)

    return LiteLLMModel(declared, options.get("api_key"), connect)


async def _disconnect(connection: Connection) -> None:
    await connection.close()

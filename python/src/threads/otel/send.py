"""One OTLP/HTTP JSON POST. The collector's answer decides the error: a network error, a
timeout, 408, 429 or 5xx may pass (collector_unavailable); any other 4xx won't
(collector_rejected). Header values are credentials: they never appear in a message."""

import gzip
from typing import Final

import httpx

from threads.otel.env import Config
from threads.result import Err, Ok
from threads.telemetry import SyncError

_BODY_LIMIT: Final = 500
_RETRYABLE: Final = frozenset({408, 429})


def _unavailable(config: Config, why: str, status: int | None = None) -> SyncError:
    message = f"the collector at {config.endpoint} is unavailable: {why}"
    return SyncError("collector_unavailable", message, status)


async def post(config: Config, body: bytes) -> Ok[None] | Err[SyncError]:
    headers = {**config.headers, "content-type": "application/json"}
    if config.compression == "gzip":
        headers["content-encoding"] = "gzip"
        body = gzip.compress(body)
    try:
        async with httpx.AsyncClient(timeout=config.timeout_ms / 1000) as client:
            response = await client.post(config.endpoint, content=body, headers=headers)
    except httpx.TimeoutException:
        return Err(_unavailable(config, "timed out"))
    except httpx.HTTPError:
        return Err(_unavailable(config, "no answer"))
    status = response.status_code
    if 200 <= status < 300:  # noqa: PLR2004 - the 2xx range
        return Ok(None)
    if status in _RETRYABLE or status >= 500:  # noqa: PLR2004 - server errors
        return Err(_unavailable(config, f"HTTP {status}", status))
    start = response.content[:_BODY_LIMIT].decode("utf-8", errors="ignore")
    message = f"the collector at {config.endpoint} rejected the spans (HTTP {status}): {start}"
    return Err(SyncError("collector_rejected", message, status))

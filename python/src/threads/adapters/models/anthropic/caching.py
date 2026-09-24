"""Prompt caching: the factory's `prompt_cache` option becomes adapter setting `prompt_cache`
(line 0), and the request carries cache controls derived from that setting alone. One TTL per
request, so every cache write is billed at the one cache_write price."""

from collections.abc import Mapping, Sequence
from typing import Final, Literal

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.config import ConfigError
from threads.log import Price
from threads.loop.model import Cache

type Ttl = Literal["5m", "1h"]

_TTL_MS: Final[Mapping[Ttl, int]] = {"5m": 300_000, "1h": 3_600_000}
_WRITE_RATIO: Final[Mapping[Ttl, tuple[int, int]]] = {"5m": (5, 4), "1h": (2, 1)}
"""Cache-write price per input price, as a fraction: 1.25 for 5m, 2 for 1h."""


def prompt_cache(given: object, hosted: Sequence[Mapping[str, JsonValue]]) -> Ttl | None:
    """The option, checked at runtime: an untyped caller may pass anything."""
    if given is False:
        return None
    ttl: Ttl
    if given == "5m":
        ttl = "5m"
    elif given == "1h":
        ttl = "1h"
    else:
        raise ConfigError(
            "invalid_config",
            f'anthropic prompt_cache must be "5m", "1h" or False, not {given!r}',
        )
    if ttl == "1h" and hosted:
        raise ConfigError(
            "invalid_config",
            "prompt_cache 1h can't be combined with hosted_tools: Anthropic caches their results "
            "for 5 minutes, so writes would be billed at two rates",
        )
    return ttl


def refuse_cache_control(
    params: Mapping[str, JsonValue], hosted: Sequence[Mapping[str, JsonValue]]
) -> None:
    """cache_control anywhere in params or a hosted tool would add a second source of cache
    writes."""
    where = "params" if _mentions(dict(params)) else None
    if where is None and any(_mentions(dict(t)) for t in hosted):
        where = "hosted_tools"
    if where is not None:
        raise ConfigError(
            "invalid_config", f"anthropic {where} can't set cache_control: pass prompt_cache"
        )


def _mentions(value: JsonValue) -> bool:
    if isinstance(value, list):
        return any(_mentions(v) for v in value)
    if isinstance(value, dict):
        return any(k == "cache_control" or _mentions(v) for k, v in value.items())
    return False


def cache_info(ttl: Ttl | None) -> Cache:
    """The model's declared cache lifetime (spec/api.json ModelInfo.cache)."""
    return "none" if ttl is None else {"ttl_ms": _TTL_MS[ttl]}


def cache_price(price: Price | None, ttl: Ttl | None) -> Price | None:
    """Cache prices a caching model's price leaves out, rounded up to whole nano-units: a write
    follows the TTL, and a read is 0.1 x input, the highest read rate Anthropic documents, so a
    cost can be overstated but never understated. Explicit prices win."""
    if price is None or ttl is None:
        return price
    num, den = _WRITE_RATIO[ttl]
    derived: dict[str, int] = {}
    if price.cache_read is MISSING:
        derived["cache_read"] = -(-price.input // 10)
    if price.cache_write is MISSING:
        derived["cache_write"] = -(-price.input * num // den)
    return price.model_copy(update=derived)


def cache_control(ttl: Ttl) -> dict[str, JsonValue]:
    """The wire cache control for a TTL; 5m is the provider's default, so it is left implicit."""
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": ttl}

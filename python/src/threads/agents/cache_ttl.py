"""The default context.cache_ttl_ms from the models' declared cache lifetimes (ModelInfo.cache).
cache_breaks() judges every response of a thread by one TTL, so the default must hold for every
model the thread can use: the primary and its fallbacks."""

from collections.abc import Iterable

from threads.agents.config import ConfigError
from threads.loop.model import Model


def agreed_cache_ttl(models: Iterable[Model]) -> int | None:
    """The TTL every caching model declares, or None when none caches. Raises invalid_config when
    a model's lifetime is unknown or two models disagree: context.cache_ttl_ms must decide."""
    agreed: tuple[Model, int] | None = None
    for model in models:
        cache = model.info.cache
        if cache == "none":
            continue
        if cache is None:
            raise ConfigError(
                "invalid_config",
                f"the cache lifetime of {_name(model)} is unknown: set context.cache_ttl_ms, or "
                "pass cache_ttl_ms to its factory",
            )
        ttl = cache["ttl_ms"]
        if agreed is None:
            agreed = (model, ttl)
        elif agreed[1] != ttl:
            raise ConfigError(
                "invalid_config",
                f"{_name(agreed[0])} caches for {_span(agreed[1])} but fallback {_name(model)} "
                f"for {_span(ttl)}: set context.cache_ttl_ms",
            )
    return None if agreed is None else agreed[1]


def _name(model: Model) -> str:
    ref = model.info.model
    return f"{ref.provider}/{ref.name}"


def _span(ms: int) -> str:
    if ms % 3_600_000 == 0:
        return f"{ms // 3_600_000}h"
    if ms % 60_000 == 0:
        return f"{ms // 60_000}m"
    return f"{ms} ms"

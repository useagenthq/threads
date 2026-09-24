"""`usd()` (spec/api.json): dollars as the integer nano-dollars budgets and prices use."""

import math

from threads.agents.config import ConfigError

_MAX_SAFE = 2**53 - 1


def usd(dollars: float) -> int:
    """usd(0.5) == 500_000_000, rounded half up to the nearest nano-dollar (the same IEEE
    operations as TypeScript's). Raises ConfigError invalid_config for a negative, non-finite
    or out-of-range amount."""
    if isinstance(dollars, bool) or not math.isfinite(dollars) or dollars < 0:
        raise ConfigError("invalid_config", f"usd needs a finite amount >= 0, got {dollars!r}")
    nanos = math.floor(dollars * 1_000_000_000 + 0.5)
    if nanos > _MAX_SAFE:
        raise ConfigError("invalid_config", f"usd({dollars!r}) is more nano-dollars than 2**53-1")
    return nanos

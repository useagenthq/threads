"""`mem0()`: refused at setup (`transport_fence_unsupported`).

The official mem0ai SDK's `AsyncMemoryClient` validates its key in the constructor with a
blocking `requests` call and sends telemetry, both outside any httpx client we can fence. A
provider whose requests can't all pass the run's fence at the real send point is never run with
a weaker one, so the extra carries no SDK until one can be fenced.
"""

from typing import NoReturn

from threads.agents.config import ConfigError
from threads.secrets import Secret


def mem0(*, api_key: Secret | None = None) -> NoReturn:
    raise ConfigError(
        "transport_fence_unsupported",
        "mem0: the mem0ai client sends outside a fenceable transport (a requests key check "
        "and telemetry in its constructor); use supermemory(), zep() or a custom provider",
    )

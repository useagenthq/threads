"""threads: an event-log agent framework."""

from typing import Final

VERSION: Final[str] = "0.0.0"
"""The package version. Mirrors `VERSION` exported by the TypeScript core."""

__version__: Final[str] = VERSION

# The public surface imports the store, which reads VERSION above: keep these after it.
from threads.agents.agent import Agent, RunStream, agent  # noqa: E402
from threads.agents.config import ConfigError, ConfigErrorCode  # noqa: E402
from threads.agents.context import RunContext  # noqa: E402
from threads.agents.results import (  # noqa: E402
    BudgetExhausted,
    Cancelled,
    Completed,
    DeltaItem,
    EventItem,
    Failed,
    HandedOff,
    Parked,
    RunError,
    RunResult,
    StatusItem,
    StreamEvent,
    Thread,
)
from threads.agents.store import Store, sqlite  # noqa: E402
from threads.agents.tool import Reconcile, Tool, tool  # noqa: E402
from threads.loop.model import (  # noqa: E402
    LookupResult,
    Model,
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
)
from threads.loop.scripted import scripted_model  # noqa: E402

__all__ = [
    "VERSION",
    "Agent",
    "BudgetExhausted",
    "Cancelled",
    "Completed",
    "ConfigError",
    "ConfigErrorCode",
    "DeltaItem",
    "EventItem",
    "Failed",
    "HandedOff",
    "LookupResult",
    "Model",
    "ModelChunk",
    "ModelInfo",
    "ModelRequest",
    "ModelResponse",
    "Parked",
    "Reconcile",
    "RunContext",
    "RunError",
    "RunResult",
    "RunStream",
    "StatusItem",
    "Store",
    "StreamEvent",
    "Thread",
    "Tool",
    "__version__",
    "agent",
    "scripted_model",
    "sqlite",
    "tool",
]

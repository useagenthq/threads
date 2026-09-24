"""threads: an event-log agent framework."""

from typing import Final

VERSION: Final[str] = "0.0.0"
"""The package version. Mirrors `VERSION` exported by the TypeScript core."""

__version__: Final[str] = VERSION

# The public surface imports the store, which reads VERSION above: keep these after it.
from threads.agents.agent import Agent, RunStream  # noqa: E402
from threads.agents.config import ConfigError, ConfigErrorCode, Failure  # noqa: E402
from threads.agents.context import RunContext  # noqa: E402
from threads.agents.factory import agent  # noqa: E402
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
from threads.agents.setup import SetsUp  # noqa: E402
from threads.agents.skills import Skill  # noqa: E402
from threads.agents.store import Store, sqlite  # noqa: E402
from threads.agents.team_agent import TeamAgent, TeamRunStream  # noqa: E402
from threads.agents.team_results import TeamRef, TeamRunResult  # noqa: E402
from threads.agents.team_tools import (  # noqa: E402
    SendRefusal,
    SendResult,
    StartRefusal,
    StartResult,
)
from threads.agents.tool import Reconcile, Tool, tool  # noqa: E402
from threads.hooks.extension import Extension, extension  # noqa: E402
from threads.hooks.types import Hooks  # noqa: E402
from threads.log import MemberRef, Principal  # noqa: E402
from threads.loop.model import (  # noqa: E402
    LooksUp,
    LookupResult,
    Model,
    ModelChunk,
    ModelContext,
    ModelInfo,
    ModelRequest,
    ModelResponse,
)
from threads.loop.scripted import scripted_model  # noqa: E402
from threads.memory.local_knowledge import local_knowledge  # noqa: E402
from threads.memory.local_memory import local_memory  # noqa: E402
from threads.memory.protocol import DeclaresWrites, KnowledgeProvider, MemoryProvider  # noqa: E402
from threads.memory.types import (  # noqa: E402
    Binding,
    Doc,
    DocVersion,
    KnowledgeHit,
    KnowledgeSource,
    MemoryHit,
    MemoryRecord,
    ProviderError,
    RecordRef,
    Scope,
)
from threads.sandbox import (  # noqa: E402
    ExecOutput,
    ExecResult,
    LooksUpSandbox,
    LooksUpSnapshot,
    Sandbox,
    SandboxInfo,
    SandboxSession,
    fake_sandbox,
)
from threads.secrets import Secret, secret  # noqa: E402
from threads.telemetry import Exporter, SkippedBranch, SyncReport  # noqa: E402
from threads.thread.case import CaseExpectation, SavedCase  # noqa: E402
from threads.thread.handle import open_thread  # noqa: E402
from threads.web.search import SearchBackend, SearchHit  # noqa: E402

__all__ = [
    "VERSION",
    "Agent",
    "Binding",
    "BudgetExhausted",
    "Cancelled",
    "CaseExpectation",
    "Completed",
    "ConfigError",
    "ConfigErrorCode",
    "DeclaresWrites",
    "DeltaItem",
    "Doc",
    "DocVersion",
    "EventItem",
    "ExecOutput",
    "ExecResult",
    "Exporter",
    "Extension",
    "Failed",
    "Failure",
    "HandedOff",
    "Hooks",
    "KnowledgeHit",
    "KnowledgeProvider",
    "KnowledgeSource",
    "LooksUp",
    "LooksUpSandbox",
    "LooksUpSnapshot",
    "LookupResult",
    "MemberRef",
    "MemoryHit",
    "MemoryProvider",
    "MemoryRecord",
    "Model",
    "ModelChunk",
    "ModelContext",
    "ModelInfo",
    "ModelRequest",
    "ModelResponse",
    "Parked",
    "Principal",
    "ProviderError",
    "Reconcile",
    "RecordRef",
    "RunContext",
    "RunError",
    "RunResult",
    "RunStream",
    "Sandbox",
    "SandboxInfo",
    "SandboxSession",
    "SavedCase",
    "Scope",
    "SearchBackend",
    "SearchHit",
    "Secret",
    "SendRefusal",
    "SendResult",
    "SetsUp",
    "Skill",
    "SkippedBranch",
    "StartRefusal",
    "StartResult",
    "StatusItem",
    "Store",
    "StreamEvent",
    "SyncReport",
    "TeamAgent",
    "TeamRef",
    "TeamRunResult",
    "TeamRunStream",
    "Thread",
    "Tool",
    "__version__",
    "agent",
    "extension",
    "fake_sandbox",
    "local_knowledge",
    "local_memory",
    "open_thread",
    "scripted_model",
    "secret",
    "sqlite",
    "tool",
]

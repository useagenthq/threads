"""threads: an event-log agent framework."""

from typing import Final

VERSION: Final[str] = "0.0.0"
"""The package version. Mirrors `VERSION` exported by the TypeScript core."""

__version__: Final[str] = VERSION

# The public surface imports the store, which reads VERSION above: keep these after it.
from threadsai._generated.eval_v1 import (  # noqa: E402
    EvalCaseResult,
    EvalReport,
    UserTurn,
    Verdict,
)
from threadsai.agents.agent import Agent, RunStream  # noqa: E402
from threadsai.agents.config import ConfigError, ConfigErrorCode, Failure  # noqa: E402
from threadsai.agents.context import RunContext  # noqa: E402
from threadsai.agents.dynamic import dynamic_agent  # noqa: E402
from threadsai.agents.dynamic_agent import DynamicAgent  # noqa: E402
from threadsai.agents.factory import agent  # noqa: E402
from threadsai.agents.member_results import StoreCorruptError  # noqa: E402
from threadsai.agents.open_team import open_team  # noqa: E402
from threadsai.agents.results import (  # noqa: E402
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
from threadsai.agents.setup import SetsUp  # noqa: E402
from threadsai.agents.skills import Skill  # noqa: E402
from threadsai.agents.store import Store, sqlite  # noqa: E402
from threadsai.agents.team_agent import TeamAgent, TeamRunStream  # noqa: E402
from threadsai.agents.team_answers import (  # noqa: E402
    AskOutcome,
    AskRefusal,
    AskResult,
    CancelRefusal,
    CancelResult,
    MemberResult,
    MonitorResult,
    ObserveRefusal,
    ReplyRefusal,
    ReplyResult,
    Waited,
    WaitResult,
)
from threadsai.agents.team_feed import InvalidCursorError  # noqa: E402
from threadsai.agents.team_handle import Team  # noqa: E402
from threadsai.agents.team_handle_types import (  # noqa: E402
    AskStatus,
    MemberState,
    OperatorRefusal,
    TeamAskResult,
    TeamCancelResult,
    TeamCursor,
    TeamItem,
    TeamMember,
    TeamRef,
    TeamSendResult,
    TeamSource,
    TeamStartResult,
    TeamWaitResult,
)
from threadsai.agents.team_results import TeamRunResult  # noqa: E402
from threadsai.agents.team_tools import (  # noqa: E402
    SendRefusal,
    SendResult,
    StartRefusal,
    StartResult,
)
from threadsai.agents.tool import Reconcile, Tool, tool  # noqa: E402
from threadsai.agents.usd import usd  # noqa: E402
from threadsai.evals.live import Live  # noqa: E402
from threadsai.evals.run import run_evals  # noqa: E402
from threadsai.hooks.extension import Extension, extension  # noqa: E402
from threadsai.hooks.types import Hooks  # noqa: E402
from threadsai.log import MemberRef, Principal, StoredMemberResult  # noqa: E402
from threadsai.loop.guard import ModelBlockedError  # noqa: E402
from threadsai.loop.model import (  # noqa: E402
    LooksUp,
    LookupResult,
    Model,
    ModelChunk,
    ModelContext,
    ModelInfo,
    ModelRequest,
    ModelResponse,
)
from threadsai.loop.scripted import scripted_model  # noqa: E402
from threadsai.memory.local_knowledge import local_knowledge  # noqa: E402
from threadsai.memory.local_memory import local_memory  # noqa: E402
from threadsai.memory.protocol import (  # noqa: E402
    DeclaresWrites,
    KnowledgeProvider,
    MemoryProvider,
)
from threadsai.memory.types import (  # noqa: E402
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
from threadsai.sandbox import (  # noqa: E402
    ExecOutput,
    ExecResult,
    LooksUpSandbox,
    LooksUpSnapshot,
    Sandbox,
    SandboxInfo,
    SandboxSession,
    Trees,
    fake_sandbox,
)
from threadsai.secrets import Secret, secret  # noqa: E402
from threadsai.team.dynamic import InvalidDefinition  # noqa: E402
from threadsai.team.policy import MessagePolicyRule  # noqa: E402
from threadsai.telemetry import Exporter, SkippedBranch, SyncReport  # noqa: E402
from threadsai.thread.bundle import ExportedBundle  # noqa: E402
from threadsai.thread.case import CaseExpectation, SavedCase  # noqa: E402
from threadsai.thread.case_simulate import Simulate  # noqa: E402
from threadsai.thread.handle import open_thread  # noqa: E402
from threadsai.thread.importing import import_thread  # noqa: E402
from threadsai.web.search import SearchBackend, SearchHit  # noqa: E402
from threadsai.workspace import Workspace, WorkspaceGit  # noqa: E402

__all__ = [
    "VERSION",
    "Agent",
    "AskOutcome",
    "AskRefusal",
    "AskResult",
    "AskStatus",
    "Binding",
    "BudgetExhausted",
    "CancelRefusal",
    "CancelResult",
    "Cancelled",
    "CaseExpectation",
    "Completed",
    "ConfigError",
    "ConfigErrorCode",
    "DeclaresWrites",
    "DeltaItem",
    "Doc",
    "DocVersion",
    "DynamicAgent",
    "EvalCaseResult",
    "EvalReport",
    "EventItem",
    "ExecOutput",
    "ExecResult",
    "ExportedBundle",
    "Exporter",
    "Extension",
    "Failed",
    "Failure",
    "HandedOff",
    "Hooks",
    "InvalidCursorError",
    "InvalidDefinition",
    "KnowledgeHit",
    "KnowledgeProvider",
    "KnowledgeSource",
    "Live",
    "LooksUp",
    "LooksUpSandbox",
    "LooksUpSnapshot",
    "LookupResult",
    "MemberRef",
    "MemberResult",
    "MemberState",
    "MemoryHit",
    "MemoryProvider",
    "MemoryRecord",
    "MessagePolicyRule",
    "Model",
    "ModelBlockedError",
    "ModelChunk",
    "ModelContext",
    "ModelInfo",
    "ModelRequest",
    "ModelResponse",
    "MonitorResult",
    "ObserveRefusal",
    "OperatorRefusal",
    "Parked",
    "Principal",
    "ProviderError",
    "Reconcile",
    "RecordRef",
    "ReplyRefusal",
    "ReplyResult",
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
    "Simulate",
    "Skill",
    "SkippedBranch",
    "StartRefusal",
    "StartResult",
    "StatusItem",
    "Store",
    "StoreCorruptError",
    "StoredMemberResult",
    "StreamEvent",
    "SyncReport",
    "Team",
    "TeamAgent",
    "TeamAskResult",
    "TeamCancelResult",
    "TeamCursor",
    "TeamItem",
    "TeamMember",
    "TeamRef",
    "TeamRunResult",
    "TeamRunStream",
    "TeamSendResult",
    "TeamSource",
    "TeamStartResult",
    "TeamWaitResult",
    "Thread",
    "Tool",
    "Trees",
    "UserTurn",
    "Verdict",
    "WaitResult",
    "Waited",
    "Workspace",
    "WorkspaceGit",
    "__version__",
    "agent",
    "dynamic_agent",
    "extension",
    "fake_sandbox",
    "import_thread",
    "local_knowledge",
    "local_memory",
    "open_team",
    "open_thread",
    "run_evals",
    "scripted_model",
    "secret",
    "sqlite",
    "tool",
    "usd",
]

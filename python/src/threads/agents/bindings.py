"""What an agent's definition binds at run time: its tools as the loop's tool runner, and its
permissions as the loop's authorization."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Final, Protocol, runtime_checkable

from threads.agents.context import RunContext
from threads.log import JsonObject, Permissions, ToolCallData, ToolSpec
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.runtime import Authorize
from threads.loop.tools import Dispatched, Invocation, Termination
from threads.permissions import Call, Category, Decision, decide
from threads.reduce import Fold
from threads.reduce.fold import policy

WORKSPACE: Final = "/workspace"
PROTECTED: Final = (
    ".git/**",
    ".threads/**",
    ".claude/**",
    ".mcp.json",
    "**/.bashrc",
    "**/.zshrc",
    "**/.profile",
    "**/.gitconfig",
    "**/.ssh/**",
)
""" defaults."""
DEFAULT_PERMISSIONS: Final = Permissions(
    mode="default",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=list(PROTECTED),
    allow_bypass=False,
    plan_exit_mode="default",
)
_RANK: Final = {"allow": 0, "ask": 1, "deny": 2}
_EDITS = frozenset({"write", "edit", "apply_patch", "notebook_edit"})


class AppTool[D](Protocol):
    """What the loop needs from one app tool; `Tool` from `tool()` is one."""

    @property
    def name(self) -> str: ...

    def spec(self) -> ToolSpec: ...

    def invalid(self, input: JsonObject) -> str | None: ...

    async def run(self, input: JsonObject, ctx: RunContext[D]) -> Dispatched: ...

    async def lookup(self, effect_key: str, ctx: RunContext[D]) -> LookupResult[str]: ...


type Fence = Callable[[], Awaitable[bool]]
"""Whether this run still owns its branch: re-checked at a tool server's real send point."""


@runtime_checkable
class ToolServer(Protocol):
    """A source of tools resolved at run setup, such as an MCP server (`threads.mcp.mcp`). Its
    connection lives for the run and every request it writes passes the fence first."""

    @property
    def name(self) -> str: ...

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]: ...


class AppTools[D]:
    """The agent's tools on the host. A tool the log names but this agent doesn't bind (a
    changed definition, an MCP tool) fails closed before any effect."""

    def __init__(self, tools: Sequence[AppTool[D]], ctx: RunContext[D]) -> None:
        self._tools: Mapping[str, AppTool[D]] = {t.name: t for t in tools}
        self._ctx = ctx

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def _context(self, call: Invocation) -> RunContext[D]:
        c = self._ctx
        return RunContext(
            c.deps, c.thread_id, c.branch_id, c.principal, call.call_id, call.effect_key
        )

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        bound = self._tools.get(spec.name)
        return f"unsupported: no binding for {spec.name}" if bound is None else bound.invalid(input)

    async def dispatch(self, call: Invocation) -> Dispatched:
        return await self._tools[call.spec.name].run(call.input, self._context(call))

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        bound = self._tools.get(call.spec.name)
        if bound is None or call.spec.effect_class != "reconcilable":
            return LookupUnknown(f"{call.spec.name} has no lookup")
        return await bound.lookup(call.effect_key, self._context(call))

    async def terminate(self, call: Invocation) -> Termination:
        # Host tools start no sandbox process group, so none can be confirmed gone.
        return "unknown"

    def provider_now(self) -> int | None:
        return None


def category(spec: ToolSpec) -> Category:
    """Web and browser tools count as other: a URL can carry data out."""
    if spec.name in _EDITS:
        return "edit"
    web = spec.name in ("web_fetch", "web_search") or spec.name.startswith("browser_")
    return "read_only" if spec.effect_class == "read_only" and not web else "other"


def permissions(fold: Fold) -> Permissions:
    """The pinned permissions, or the defaults."""
    pinned = policy(fold)
    if pinned is not None and isinstance(pinned.permissions, Permissions):
        return pinned.permissions
    return DEFAULT_PERMISSIONS


def authorize(fold: Fold, call: ToolCallData, spec: ToolSpec) -> Decision:
    """the fold against the pinned permissions, the current mode and the thread's remembered
    rules."""
    request = Call(spec.name, category(spec), dict(call.input))
    return decide(permissions(fold), WORKSPACE, fold.mode, request, thread_rules=fold.thread_rules)


def capped(ceilings: Sequence[Permissions]) -> Authorize:
    """Each call decided under the thread's own policy and under every ceiling (each ancestor of
    a subagent, a handoff target's principal and host), each in its own mode; the strictest wins."""
    if not ceilings:
        return authorize

    def decide_all(fold: Fold, call: ToolCallData, spec: ToolSpec) -> Decision:
        request = Call(spec.name, category(spec), dict(call.input))
        decided = authorize(fold, call, spec)
        for ceiling in ceilings:
            cap = decide(ceiling, WORKSPACE, ceiling.mode, request)
            decided = cap if _RANK[cap.decision] > _RANK[decided.decision] else decided
        return decided

    return decide_all

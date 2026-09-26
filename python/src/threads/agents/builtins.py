"""The built-in sandbox tools bound to a run: the branch's sandbox session,
opened through the resource ledger, and one tool runner that routes each call to the built-ins
or the app tools by name."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Final, Literal

from threads.agents.bindings import AppTools
from threads.log import BranchId, JsonObject, ParseError, ToolSpec
from threads.loop.defaults import context
from threads.loop.model import LookupResult
from threads.loop.tools import Dispatched, Invocation, Termination, ToolRunner
from threads.memory.types import Outcome
from threads.result import Err, Ok
from threads.sandbox.ledger import (
    Fenced,
    Tracked,
    abandon,
    acquire,
    release_session,
    session_lookup,
)
from threads.sandbox.protocol import (
    Sandbox,
    SandboxContext,
    SandboxError,
    SandboxSession,
    Trees,
    WorkspaceRefusal,
)
from threads.sandbox.tree.tree import Tree
from threads.sandbox.trees import place_tree
from threads.store import SqliteStore, Writer
from threads.store.worker import Clock
from threads.thread.snapshot import take_snapshot
from threads.tools import FRAMEWORK, HOST, SANDBOXED, ReadResults, SandboxTools
from threads.tools.runner import NO_SERVERS, parse
from threads.tools.specs import PROVIDED

NO_GATEWAYS: Final[Mapping[str, ToolRunner]] = MappingProxyType({})

_LOST_PLACE: Final = frozenset({"stale_epoch", "cleanup_claim_lost", "unavailable", "timeout"})
"""Placement failures that are the transport's, not the workspace's."""

type Egress = Sequence[str] | Literal["unenforced"]


def egress_denied(egress: Egress) -> bool:
    """Deny-all: an empty allowlist."""
    return egress != "unenforced" and not egress


async def _settle_pending(by: Fenced, sandbox: Sandbox, branch: BranchId) -> None:
    """Settles this branch's pending sandbox rows before a new create. A crash between the
    create and the row going live (placing a workspace happens in that window) leaves one
    behind, and a reattach reads only live rows while gc skips pending. Capture-scratch and
    fork rows are begun the same way, so this settles those too: safe, because the lease holder
    doing the settling is the only one who could be using them. Best effort: `abandon` releases
    what it finds and parks what it can't prove, and neither blocks the new create."""
    for row in await by.ledger.rows("pending"):
        if row.owner_branch_id == branch and row.kind == "sandbox":
            await abandon(by, sandbox, row)


async def _place(
    session: SandboxSession, tree: Tree, store: SqliteStore, ctx: SandboxContext
) -> SandboxError | WorkspaceRefusal | None:
    """Places the pinned workspace tree in a fresh sandbox; the failure a tool call gets."""
    if not isinstance(session, Trees):
        return WorkspaceRefusal(
            "capability_missing",
            "workspace: this sandbox's sessions can't import a tree into /workspace",
        )
    placed = await place_tree(session, tree, store.read_artifact, ctx)
    if isinstance(placed, Ok):
        return None
    error = placed.error
    if isinstance(error, SandboxError) and error.code in _LOST_PLACE:
        return error
    return WorkspaceRefusal("workspace_mismatch", error.message)


async def open_session(
    store: SqliteStore,
    sandbox: Sandbox,
    writer: Writer,
    clock: Clock,
    workspace: Tree | None = None,
) -> Ok[SandboxSession] | Err[SandboxError | ParseError | WorkspaceRefusal]:
    """The branch's live sandbox, reattached; else a new one whose ledger row is durable before
    the provider call. A thread with workspace inputs (lane 16 E) has the pinned tree placed
    into the new sandbox before its row goes live, so no tool can reach a sandbox whose
    /workspace isn't the pinned one."""
    ctx = store.context(writer.owner, clock)
    for row in await store.ledger.rows("live"):
        if row.owner_branch_id == writer.branch_id and row.kind == "sandbox" and row.ref:
            return await sandbox.attach(row.ref, ctx)
    by = Fenced(store.ledger, writer.owner, ctx, clock)
    await _settle_pending(by, sandbox, writer.branch_id)
    refused: SandboxError | WorkspaceRefusal | None = None

    async def ready(session: SandboxSession) -> None:
        nonlocal refused
        if workspace is not None:
            refused = await _place(session, workspace, store, ctx)

    how = Tracked(
        "sandbox",
        lambda key: sandbox.create(key, ctx),
        *session_lookup(sandbox, ctx),
        lambda session: session.id,
        ready,
    )
    made = await acquire(store.ledger, writer.owner, sandbox.info.provider, how, clock)
    if isinstance(made, Err):
        return made
    row, session = made.value
    if refused is None:
        return Ok(session)
    await release_session(by, row, session)
    return Err(refused)


def sandbox_tools(  # noqa: PLR0913, PLR0917 - one binding, its clock and what it places
    store: SqliteStore,
    sandbox: Sandbox,
    writer: Writer,
    clock: Clock,
    servers: Mapping[str, Sequence[str]] = NO_SERVERS,
    workspace: Tree | None = None,
) -> SandboxTools:
    """`servers`: the lsp tool's declared languages and their server commands. `workspace`: the
    pinned tree every sandbox this thread creates starts from."""
    return SandboxTools(
        lambda: open_session(store, sandbox, writer, clock, workspace),
        store.context(writer.owner, clock),
        store,
        lambda: context(writer.fold).spill,
        servers,
    )


async def snapshot_turn_end(  # noqa: PLR0913 - the corpus revision rides with the capture
    store: SqliteStore,
    writer: Writer,
    sandbox: Sandbox,
    tools: SandboxTools,
    clock: Clock,
    *,
    knowledge_revision: Callable[[], Awaitable[Outcome[int] | None]] | None = None,
) -> None:
    """The end-of-turn snapshot policy: a turn that used the sandbox ends at a fork point. The
    capture goes through `take_snapshot` (quiescence under the writer, ledger rows, the image
    proven by a restore); nothing else appends a snapshot. The run's loop has returned, so no
    append or dispatch can interleave. A refused capture appends nothing, and so does a failed
    knowledge revision: a knowledge-bound snapshot always records one. The revision is read
    only when a capture follows, and before it, so a failure costs no scratch sandbox."""
    opened = tools.opened
    if opened is None or not sandbox.info.capture_classes:
        return
    read = None if knowledge_revision is None else await knowledge_revision()
    if isinstance(read, Err):
        return
    revision = None if read is None else read.value
    await take_snapshot(store, writer, sandbox, opened, clock, knowledge_revision=revision)


class Routed:
    """Sandbox built-ins go to the sandbox, read_tool_result to the host reader, the web and git
    gateway tools to their runners, memory and knowledge tools to their providers, extension
    tools to theirs, every other name to the app tools."""

    def __init__[D](  # noqa: PLR0913 - one runner per tool source
        self,
        sandbox: ToolRunner | None,
        results: ReadResults,
        app: ToolRunner,
        provided: ToolRunner | None = None,
        ext: AppTools[D] | None = None,
        *,
        gateways: Mapping[str, ToolRunner] = NO_GATEWAYS,
    ) -> None:
        self._gateways = gateways
        self._sandbox = sandbox
        self._results = results
        self._app = app
        # Outside a team, send and start are free names: an app tool may take one.
        self._own = app.names if isinstance(app, AppTools) else frozenset[str]()
        self._provided = provided
        self._ext: ToolRunner | None = ext
        self._ext_names = frozenset[str]() if ext is None else ext.names

    def _for(self, name: str) -> ToolRunner:
        if name in self._gateways:
            return self._gateways[name]
        if name in HOST:
            return self._results
        if name in PROVIDED and self._provided is not None:
            return self._provided
        if name in SANDBOXED and self._sandbox is not None:
            return self._sandbox
        if self._ext is not None and name in self._ext_names:
            return self._ext
        return self._app

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        if spec.name in FRAMEWORK and spec.name not in self._own:
            parsed = parse(spec.name, input)
            return parsed.error if isinstance(parsed, Err) else None
        return self._for(spec.name).invalid(spec, input)

    async def dispatch(self, call: Invocation) -> Dispatched:
        return await self._for(call.spec.name).dispatch(call)

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return await self._for(call.spec.name).lookup(call)

    async def terminate(self, call: Invocation) -> Termination:
        return await self._for(call.spec.name).terminate(call)

    def provider_now(self) -> int | None:
        # Only app tools are idempotent; built-ins never dedup.
        return self._app.provider_now()

"""The built-in sandbox tools bound to a run: the branch's sandbox session,
opened through the resource ledger, and one tool runner that routes each call to the built-ins
or the app tools by name."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Final, Literal

from threads.agents.bindings import AppTools
from threads.log import JsonObject, ParseError, ToolSpec
from threads.loop.defaults import context
from threads.loop.model import LookupResult
from threads.loop.tools import Dispatched, Invocation, Termination, ToolRunner
from threads.memory.types import Outcome
from threads.result import Err, Ok
from threads.sandbox.ledger import Tracked, acquire, session_lookup
from threads.sandbox.protocol import Sandbox, SandboxError, SandboxSession
from threads.store import SqliteStore, Writer
from threads.store.worker import Clock
from threads.thread.snapshot import take_snapshot
from threads.tools import FRAMEWORK, HOST, SANDBOXED, ReadResults, SandboxTools
from threads.tools.runner import NO_SERVERS, parse
from threads.tools.specs import PROVIDED

NO_GATEWAYS: Final[Mapping[str, ToolRunner]] = MappingProxyType({})

type Egress = Sequence[str] | Literal["unenforced"]


def egress_denied(egress: Egress) -> bool:
    """Deny-all: an empty allowlist."""
    return egress != "unenforced" and not egress


async def open_session(
    store: SqliteStore, sandbox: Sandbox, writer: Writer, clock: Clock
) -> Ok[SandboxSession] | Err[SandboxError | ParseError]:
    """The branch's live sandbox, reattached; else a new one whose ledger row is durable before
    the provider call."""
    ctx = store.context(writer.owner, clock)
    for row in await store.ledger.rows("live"):
        if row.owner_branch_id == writer.branch_id and row.kind == "sandbox" and row.ref:
            return await sandbox.attach(row.ref, ctx)
    how = Tracked(
        "sandbox",
        lambda key: sandbox.create(key, ctx),
        *session_lookup(sandbox, ctx),
        lambda session: session.id,
    )
    made = await acquire(store.ledger, writer.owner, sandbox.info.provider, how, clock)
    return made if isinstance(made, Err) else Ok(made.value[1])


def sandbox_tools(
    store: SqliteStore,
    sandbox: Sandbox,
    writer: Writer,
    clock: Clock,
    servers: Mapping[str, Sequence[str]] = NO_SERVERS,
) -> SandboxTools:
    """`servers`: the lsp tool's declared languages and their server commands."""
    return SandboxTools(
        lambda: open_session(store, sandbox, writer, clock),
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

    def __init__(  # noqa: PLR0913 - one runner per tool source
        self,
        sandbox: ToolRunner | None,
        results: ReadResults,
        app: ToolRunner,
        provided: ToolRunner | None = None,
        ext: AppTools[None] | None = None,
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
        self._ext = ext

    def _for(self, name: str) -> ToolRunner:
        if name in self._gateways:
            return self._gateways[name]
        if name in HOST:
            return self._results
        if name in PROVIDED and self._provided is not None:
            return self._provided
        if name in SANDBOXED and self._sandbox is not None:
            return self._sandbox
        if self._ext is not None and name in self._ext.names:
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

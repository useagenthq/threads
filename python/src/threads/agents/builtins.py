"""The built-in sandbox tools bound to a run: the branch's sandbox session,
opened through the resource ledger, and one tool runner that routes each call to the built-ins
or the app tools by name."""

from collections.abc import Sequence
from typing import Literal

from threads.log import JsonObject, ParseError, ToolSpec
from threads.loop.defaults import context
from threads.loop.model import LookupResult
from threads.loop.tools import Dispatched, Invocation, Termination, ToolRunner
from threads.result import Err, Ok
from threads.sandbox.ledger import Tracked, acquire
from threads.sandbox.protocol import Sandbox, SandboxError, SandboxSession
from threads.store import SqliteStore, Writer
from threads.store.worker import Clock
from threads.thread.snapshot import take_snapshot
from threads.tools import HOST, SANDBOXED, ReadResults, SandboxTools

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
        lambda key: sandbox.lookup(key, ctx),
        sandbox.info.lookup.create,
        lambda session: session.id,
    )
    made = await acquire(store.ledger, writer.owner, sandbox.info.provider, how, clock)
    return made if isinstance(made, Err) else Ok(made.value[1])


def sandbox_tools(
    store: SqliteStore, sandbox: Sandbox, writer: Writer, clock: Clock
) -> SandboxTools:
    return SandboxTools(
        lambda: open_session(store, sandbox, writer, clock),
        store.context(writer.owner, clock),
        store,
        lambda: context(writer.fold).spill,
    )


async def snapshot_turn_end(
    store: SqliteStore, writer: Writer, sandbox: Sandbox, tools: SandboxTools, clock: Clock
) -> None:
    """The end-of-turn snapshot policy: a turn that used the sandbox ends at a fork point. The
    capture goes through `take_snapshot` (quiescence under the writer, ledger rows, the image
    proven by a restore); nothing else appends a snapshot. The run's loop has returned, so no
    append or dispatch can interleave. A refused capture appends nothing."""
    if tools.opened is not None and sandbox.info.capture_classes:
        await take_snapshot(store, writer, sandbox, tools.opened, clock)


class Routed:
    """Sandbox built-ins go to the sandbox, read_tool_result to the host reader, every other
    name to the app tools."""

    def __init__(self, sandbox: ToolRunner | None, results: ReadResults, app: ToolRunner) -> None:
        self._sandbox = sandbox
        self._results = results
        self._app = app

    def _for(self, name: str) -> ToolRunner:
        if name in HOST:
            return self._results
        if name in SANDBOXED and self._sandbox is not None:
            return self._sandbox
        return self._app

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
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

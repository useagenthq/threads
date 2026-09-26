"""The rerun (spec lane 22, B.2), promoted from the conformance runner, which now imports it: import
the case log into a private in-memory store on the case's clock, take the lease (recovery runs
first), send the recorded input, and loop against the recorded model replies, tool results,
stubs, hook decisions and recall until the branch is idle or parked. No sandbox, no provider."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.eval_v1 import ExtensionScript
from threads._json_schema import holds
from threads.agents.team import Lead
from threads.evals.answers import Answers
from threads.evals.case_dir import CaseSandbox
from threads.evals.replayed import Replayed, recorded_authorizer, scripted_policy
from threads.evals.stand_in import stand_ins
from threads.evals.stub_queue import stub_gateway
from threads.log import Event, ModelRef, ParseError, ToolCallData, ToolSpec, UserInputEvent
from threads.loop import gates
from threads.loop.drive import drive
from threads.loop.model import Model
from threads.loop.recovery import recover
from threads.loop.runtime import Failed, Halt, Idle, Runtime
from threads.permissions import Decision
from threads.reduce import Fold
from threads.reduce.fold import policy
from threads.result import Err
from threads.store import Draft, SqliteStore, StoredEvent, Writer, verify_export
from threads.store.artifacts import MemoryArtifacts

HOLDER = "eval-rerun"


@dataclass(frozen=True, slots=True)
class RerunInput:
    log: bytes
    artifacts: Sequence[bytes]
    now: int
    model: JsonValue
    """model.json; None: recovery only, the loop doesn't run."""
    sandbox: CaseSandbox | None
    stubs: JsonValue
    """stubs.json: every mediated call answers from it, and an unmatched one fails closed."""
    extensions: ExtensionScript | None
    input: Draft | None
    """What to send once the log is imported: the recorded user_input."""
    recorded: Sequence[Event] | None
    """The recorded turn, whose permission decisions stand; None: the conformance policy."""


@dataclass(frozen=True, slots=True)
class Refused:
    code: str
    seq: int | None


@dataclass(frozen=True, slots=True)
class Ran:
    halt: Halt | None
    appended: tuple[StoredEvent, ...]
    """Everything this run appended, acquiring included."""
    script_left: int
    unexpected: int
    counters: Mapping[str, Mapping[str, int]]
    stubs: tuple[int, int] | None
    """(consumed, unmatched)"""
    unrecorded_calls: int
    unrecorded_hooks: int
    left: int
    """Recorded results and recall no call used."""
    exported: bytes


class _Clock:
    """The case's clock: a recorded wait advances it at once; a rerun never sleeps."""

    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    async def wait_until(self, when: int) -> None:
        self.now = max(self.now, when)


def _output(fold: Fold) -> Callable[[JsonValue], str | None] | None:
    """The pinned output schema, checked with the log's keyword subset."""
    pinned = policy(fold)
    if pinned is None or pinned.output is MISSING:
        return None
    schema: JsonValue = dict(pinned.output.schema_)

    def check(value: JsonValue) -> str | None:
        try:
            return None if holds(schema, value) else "does not match the schema"
        except TypeError:
            return None

    return check


def _source(fold: Fold) -> Literal["resume", "startup"]:
    return "resume" if any(isinstance(e, UserInputEvent) for e in fold.events) else "startup"


async def _run(rt: Runtime, input: Draft | None, loops: bool) -> Halt | None:
    """Recovery first; then, as a run does, session_start, the recorded input and the loop;
    session_end observes an ended run."""
    halt = await recover(rt)
    if isinstance(halt, Failed) or not loops:
        return halt
    halt = await gates.session_start(rt, _source(rt.fold))
    if halt is not None:
        return halt
    if rt.fold.in_turn:
        halt = await drive(rt)
        if not isinstance(halt, Idle):
            return halt
    if input is not None:
        appended = await rt.append(input)
        if isinstance(appended, Err):
            return Failed("branch_not_runnable", appended.error.message)
    halt = await drive(rt)
    if not isinstance(halt, Failed):
        await gates.observe(rt, "session_end")
    return halt


async def rerun(given: RerunInput) -> Ran | Refused:
    clock = _Clock(given.now)
    verified = verify_export(given.log, clock())
    if isinstance(verified, Err):
        return _refused(verified.error)
    artifacts = MemoryArtifacts()
    for data in given.artifacts:
        artifacts.put(data)
    opened = await SqliteStore.open(artifacts=artifacts)
    if isinstance(opened, Err):
        raise AssertionError(f"an in-memory store opens: {opened.error.message}")
    store = opened.value
    try:
        imported = await store.import_log(verified.value)
        if isinstance(imported, Err):
            return _refused(imported.error)
        branch = verified.value.segments[-1].header.branch_id
        acquired = await store.acquire(branch, HOLDER, clock)
        if isinstance(acquired, Err) and acquired.error.code == "branch_not_runnable":
            acquired = await store.repair_torn(branch, HOLDER, clock)
        if isinstance(acquired, Err):
            return _refused(acquired.error)
        return await _ran(given, store, acquired.value, clock, artifacts, verified.value.fold.seq)
    finally:
        await store.close()


def _v1(given: RerunInput) -> Mapping[str, Mapping[str, JsonValue]]:
    sandbox = given.sandbox
    return {} if sandbox is None or sandbox.v1 is None else sandbox.v1


def authorizer(given: RerunInput) -> Callable[[Fold, ToolCallData, ToolSpec], Decision]:
    """A recording's own permission decisions; the corpus's scripted policy otherwise."""
    if given.recorded is None:
        return scripted_policy(_v1(given))
    return recorded_authorizer(given.recorded)


def _concurrent(given: RerunInput) -> frozenset[str]:
    """The corpus's tools its scripted sandbox declares concurrent."""
    return frozenset(n for n, t in _v1(given).items() if t.get("concurrent") is True)


async def _ran(  # noqa: PLR0913, PLR0917 - the run and what it compares after
    given: RerunInput,
    store: SqliteStore,
    writer: Writer,
    clock: _Clock,
    artifacts: MemoryArtifacts,
    before: int,
) -> Ran:
    fold = writer.fold
    pinned = policy(fold)
    limits = () if pinned is None or pinned.models is MISSING else tuple(pinned.models)
    empty: dict[str, JsonValue] = {"responses": []}
    script = given.model if isinstance(given.model, dict) else empty
    replay = Replayed(script, limits)
    as_recorded = given.recorded is not None

    def models(ref: ModelRef) -> Model:
        return replay.as_pinned(ref) if as_recorded else replay

    # Offline the rerun answers only from the saved turn's entries: a simulated case's prefix
    # stubs are the live re-drive's, and lane 22's "no stub left over" rule is unchanged.
    stubs = None if not isinstance(given.stubs, dict) else stub_gateway(given.stubs, "turn")
    tools = Answers(
        given.sandbox,
        [] if given.extensions is None else list(given.extensions.recall),
        stubs,
        artifacts.get,
        clock,
    )
    hooks = stand_ins(given.extensions)
    rt = Runtime(
        store,
        writer,
        models,
        tools,
        authorizer(given),
        clock,
        clock.wait_until,
        _output(fold),
        hooks=hooks.hooks,
        # spec/conformance/README.md recover step 5: the thread is its own team lead.
        framework=Lead("" if fold.started is None else fold.started.agent_name),
        concurrent=_concurrent(given),
    )
    halt = await _run(rt, given.input, given.model is not None)
    exported = await store.export(writer.branch_id)
    if isinstance(exported, Err):
        raise AssertionError(f"a rerun's branch exports: {exported.error.message}")
    read = verify_export(exported.value, clock())
    if isinstance(read, Err):
        raise AssertionError(f"a rerun's branch verifies: {read.error.message}")
    appended = tuple(e for e, _ in read.value.segments[-1].events if e.seq > before)
    return Ran(
        halt,
        appended,
        replay.remaining,
        replay.unexpected,
        tools.counters(),
        None if stubs is None else (stubs.consumed, stubs.unmatched),
        tools.unrecorded,
        hooks.unrecorded(),
        tools.left() + (0 if stubs is None else stubs.left),
        exported.value,
    )


def _refused(error: ParseError) -> Refused:
    return Refused(error.code, error.seq)

"""ask.complete and a wait's finish, under the asker's or waiter's writer (spec/schema/README.md,
"Teams"; design §4.8 and §4.12): one decision whatever triggers it (a consume, a cancel, the
deadline step). A member also resumes its park and records the call's one tool_result.
Reference: spec/tools/fixtures/ops_consume.py (complete) and ops_observe.py (finish)."""

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import ArtifactRef, MailEnvelope, ParkAddress, WaitStartedData
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.render.artifacts import ReadArtifact
from threads.result import Err
from threads.store.lines import Draft
from threads.team.call import ReadText, tool_result
from threads.team.mail import received
from threads.team.rows import bytes_of, mail_envelope, member_named, own_rows, pending_for
from threads.team.settle import AppendContext
from threads.team.view import ask_open, items_of, open_waits, parked_on, settle_monitors, untaken

NOTICES: Final = frozenset({"member_settled", "member_ended"})


@dataclass(frozen=True, slots=True)
class CloseContext(AppendContext):
    """A writer's append that closes asks and waits: its committed log, and where refs are read."""

    fold: Fold
    read: ReadText


def reader_of(read: ReadArtifact) -> ReadText:
    """Text read from an artifact store. A ref the verified log names is always present, so a
    missing or corrupt artifact is a broken store invariant: it raises."""

    def text(ref: ArtifactRef) -> str:
        got = read(ref.sha256)
        if isinstance(got, Err):
            raise AssertionError(f"artifact {ref.sha256}: {got.error.message}")
        return got.value.decode()

    return text


def call_of(key: str) -> str:
    """The call an ask or wait belongs to: its id after the sender branch."""
    return key.split(":", 1)[1]


def _body_text(body: JsonValue, read: ReadText) -> str:
    if isinstance(body, dict) and isinstance(text := body.get("text"), str):
        return text
    if not isinstance(body, dict):
        raise AssertionError("a text body is text or a ref")
    return read(ArtifactRef.model_validate(body.get("ref")))


def public_result(result: JsonValue, read: ReadText) -> JsonValue:
    """A stored result as the public one: a completed output is its text."""
    if isinstance(result, dict) and result.get("status") == "completed":
        return {**result, "output": _body_text(result.get("output"), read)}
    return result


def mine(ctx: CloseContext) -> list[MailEnvelope]:
    """This writer's pending mail the batch hasn't taken."""
    return untaken(pending_for(ctx.conn, own_rows(ctx.conn, ctx.thread_id)), ctx.batch)


def _resume(ctx: CloseContext, address: ParkAddress, cause: str) -> None:
    """A member's park on `address` resumes, caused by `cause`, when it is still open."""
    if not ctx.fold.team.team_log and parked_on(ctx.fold, ctx.batch, address):
        data: dict[str, JsonValue] = {"address": to_json(address), "cause_event_id": cause}
        ctx.batch.add(Draft("resumed", data))


def close_ask(
    ctx: CloseContext, ask_id: str, outcome: dict[str, JsonValue], cause: str | None
) -> JsonValue:
    """ask_closed, and for a member asker its resumed and the ask call's one result."""
    closed = ctx.batch.add(Draft("ask_closed", {"ask_id": ask_id, "outcome": outcome}))
    value = _ask_value(ctx, ask_id, outcome)
    if ctx.fold.team.team_log:
        return value
    _resume(ctx, ParkAddress(kind="ask", id=ask_id), cause or closed)
    ctx.batch.add(tool_result(call_of(ask_id), value))
    return value


def _ask_value(ctx: CloseContext, ask_id: str, outcome: dict[str, JsonValue]) -> JsonValue:
    status = outcome["status"]
    if status == "answered":
        reply = mail_envelope(ctx.conn, str(outcome["reply"]))
        if reply is None:
            raise AssertionError(f"no reply {outcome['reply']}")
        env = to_json(reply)
        if not isinstance(env, dict):
            raise AssertionError("an envelope is an object")
        text = _body_text(env.get("body"), ctx.read)
        return {"ask_id": ask_id, "status": status, "member": env["from"], "text": text}
    if status == "member_ended":
        return {
            "ask_id": ask_id,
            "status": status,
            "result": public_result(outcome["result"], ctx.read),
        }
    return {"ask_id": ask_id, "status": status}


def complete_ask(ctx: CloseContext, ask_id: str, *, cancelled: bool, due: bool) -> JsonValue:
    """ask.complete's one decision, whatever triggers it: a pending reply, then a pending bounce
    naming the ask, then a cancel (a member asker) or a closed team (the team log), then the
    deadline. None: nothing decides it yet."""
    for kind in ("reply", "bounce"):
        env = next((m for m in mine(ctx) if m.kind == kind and m.ask_id == ask_id), None)
        if env is None:
            continue
        got = ctx.batch.add(received(env))
        if kind == "reply":
            return close_ask(ctx, ask_id, {"status": "answered", "reply": env.mail_id}, got)
        if env.result is MISSING:
            raise AssertionError("an ask's bounce carries the ended member's result")
        result = to_json(env.result)
        return close_ask(ctx, ask_id, {"status": "member_ended", "result": result}, got)
    if cancelled:
        return close_ask(ctx, ask_id, {"status": "cancelled"}, None)
    return close_ask(ctx, ask_id, {"status": "timed_out"}, None) if due else None


def take_answer(ctx: CloseContext, env: MailEnvelope) -> None:
    """A reply or an ask's bounce: completes its ask while open, else is recorded only."""
    ask_id = env.ask_id if isinstance(env.ask_id, str) else ""
    if ask_open(ctx.conn, ctx.branch_id, ask_id, ctx.batch):
        complete_ask(ctx, ask_id, cancelled=False, due=False)
    else:
        ctx.batch.add(received(env))


def committed_notices(ctx: CloseContext, wait_id: str) -> None:
    """Every settle or end notice of the wait already committed counts: consume it first."""
    settles = settle_monitors(ctx.fold, ctx.batch, ctx.branch_id)
    for env in mine(ctx):
        monitor = env.monitor_id
        if env.kind in NOTICES and isinstance(monitor, str) and settles.get(monitor) == wait_id:
            ctx.batch.add(received(env))


def take_wait_notice(ctx: CloseContext, env: MailEnvelope) -> bool:
    """A settle or end notice that is a wait's: the receipt, and the wait finishes if that meets
    its mode (a notice of a wait that already finished is recorded only). False: not a wait's."""
    monitor = env.monitor_id if isinstance(env.monitor_id, str) else ""
    wait_id = settle_monitors(ctx.fold, ctx.batch, ctx.branch_id).get(monitor)
    if wait_id is None:
        return False
    got = ctx.batch.add(received(env))
    if wait_id in open_waits(ctx.fold, ctx.batch):
        finish_wait(ctx, wait_id, cause=got, deadline=False)
    return True


def _satisfied(mode: Literal["all", "any"] | int, settled: int, total: int) -> bool:
    need = total if mode == "all" else 1 if mode == "any" else mode
    return settled >= need


def _evidence(ctx: CloseContext) -> dict[str, JsonValue]:
    """The settled results this log holds (observations and received notices), by MonitorId."""
    out: dict[str, JsonValue] = {}
    types = frozenset({"member_observed", "message_received"})
    for type_, _, data in items_of(ctx.fold, ctx.batch, types):
        found = data.get("envelope", {}) if type_ == "message_received" else data
        if not isinstance(found, dict):
            continue
        monitor, result = found.get("monitor_id"), found.get("result")
        if isinstance(monitor, str) and result is not None:
            out[monitor] = result
    return out


def finish_wait(ctx: CloseContext, wait_id: str, *, cause: str | None, deadline: bool) -> JsonValue:
    """wait_finished from this log's evidence (member_observed and received notices), in the
    wait's member order, once the mode is met or at the deadline; a member waiter also resumes
    and records the call's one result. Returns the waiter's view: waiting, or waited."""
    found = next(
        (
            (event_id, WaitStartedData.model_validate(data))
            for _, event_id, data in items_of(ctx.fold, ctx.batch, frozenset({"wait_started"}))
            if data.get("wait_id") == wait_id
        ),
        None,
    )
    if found is None:
        raise AssertionError(f"no wait_started {wait_id}")
    event_id, started = found
    evidence = _evidence(ctx)
    finished: list[JsonValue] = []
    parked: list[JsonValue] = []
    pending: list[JsonValue] = []
    for m in started.members:
        got = evidence.get(f"{ctx.branch_id}:{event_id}:{m.name}")
        row = member_named(ctx.conn, m.team, m.name)
        if got is not None:
            finished.append(got)
        elif row is not None and row.state == "parked" and row.branch_id is not None:
            parked.append({"member": to_json(m), "reason": _park_reason(ctx, row.branch_id)})
        else:
            pending.append(to_json(m))
    met = _satisfied(started.mode, len(finished), len(started.members))
    if not met and not deadline:
        return {"status": "waiting", "wait_id": wait_id}
    done: dict[str, JsonValue] = {
        "finished": finished,
        "parked": parked,
        "pending": pending,
        "timed_out": not met,
    }
    finished_id = ctx.batch.add(Draft("wait_finished", {"wait_id": wait_id, **done}))
    waited: JsonValue = {
        "status": "waited",
        **done,
        "finished": [public_result(r, ctx.read) for r in finished],
    }
    if ctx.fold.team.team_log:
        return waited
    _resume(ctx, ParkAddress(kind="wait", id=wait_id), cause or finished_id)
    ctx.batch.add(tool_result(call_of(wait_id), waited))
    return waited


def _park_reason(ctx: CloseContext, branch: str) -> str:
    """Why a parked member is parked: its log's last park."""
    row: tuple[object] | None = ctx.conn.execute(
        "SELECT line FROM events WHERE branch_id = ? AND type = 'parked' ORDER BY seq DESC LIMIT 1",
        (branch,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"a parked member {branch} has a park")
    data = _LINE.validate_json(bytes_of(row[0]))["data"]
    reason = data.get("reason") if isinstance(data, dict) else None
    if not isinstance(reason, str):
        raise AssertionError("a park names its reason")
    return reason


_LINE: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

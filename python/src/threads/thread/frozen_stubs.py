"""A stub fork's frozen script (ADR 0010, spec/schema/README.md, "Portable bundles" and the
freezing rule): every run of a branch whose resolved chain holds a stub fork answers each mediated
operation from the artifact that fork recorded, so a reopened child stays stubbed in a fresh
process and later parent appends never change it."""

from collections.abc import Sequence

from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import ArtifactRef, Event, EventId, ForkEvent, ParseError
from threads.loop.stubs import Stub, parse_stubs
from threads.reduce.fold import Fold
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.thread.case_files import encode_stub_script, stub_script
from threads.thread.fork import fork_point

_SCRIPT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def stub_fork_ref(chain: Sequence[Event]) -> ArtifactRef | None:
    """The frozen script of the innermost stub fork on the chain, if the chain has one."""
    found: ArtifactRef | None = None
    for event in chain:
        if isinstance(event, ForkEvent) and event.data.stub_script_ref is not MISSING:
            found = event.data.stub_script_ref
    return found


def frozen_stubs(ref: ArtifactRef, data: bytes) -> Ok[tuple[Stub, ...]] | Err[ParseError]:
    """The stubs a stub branch runs behind, parsed from the fork event's artifact bytes and
    checked against the length the fork recorded (the store checked the hash on the read)."""
    if len(data) != ref.bytes:
        why = f"the frozen stub script {ref.sha256} is {len(data)} bytes, not {ref.bytes}"
        return Err(ParseError("artifact_corrupt", why))
    try:
        return Ok(parse_stubs(_SCRIPT.validate_json(data)))
    except (ValidationError, ValueError):
        why = f"the frozen stub script {ref.sha256} is not a stub script"
        return Err(ParseError("artifact_corrupt", why))


async def freeze_after(
    sq: SqliteStore, fold: Fold, point: EventId
) -> Ok[JsonValue] | Err[ParseError]:
    """A stub fork's frozen script: the branch's mediated calls after `point`, each with its
    complete verified output, stored as an artifact before the fork event names it. A missing or
    corrupt parent artifact fails here, so the fork leaves no child."""
    at = fork_point(fold, point)
    if isinstance(at, Err):
        return at
    after = [e for e in fold.events if e.seq > at.value.seq]
    script = await stub_script(after, sq.get_artifact)
    if isinstance(script, Err):
        return script
    data = encode_stub_script(script.value)
    ref: JsonValue = {
        "sha256": await sq.put_artifact(data),
        "bytes": len(data),
        "media_type": "application/json",
    }
    return Ok(ref)

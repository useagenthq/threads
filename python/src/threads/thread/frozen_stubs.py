"""A stub fork's frozen script (ADR 0010, spec/schema/README.md, "Portable bundles" and the
freezing rule): every run of a branch whose resolved chain holds a stub fork answers each mediated
operation from the artifact that fork recorded, so a reopened child stays stubbed in a fresh
process and later parent appends never change it."""

from collections.abc import Sequence

from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import ArtifactRef, Event, ForkEvent, ParseError
from threads.loop.stubs import Stub, parse_stubs
from threads.result import Err, Ok

_SCRIPT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def stub_fork_ref(chain: Sequence[Event]) -> ArtifactRef | None:
    """The frozen script of the innermost stub fork on the chain, if the chain has one."""
    found: ArtifactRef | None = None
    for event in chain:
        if isinstance(event, ForkEvent) and event.data.stub_script_ref is not MISSING:
            found = event.data.stub_script_ref
    return found


def frozen_stubs(chain: Sequence[Event], data: bytes) -> Ok[tuple[Stub, ...]] | Err[ParseError]:
    """The stubs a stub branch runs behind, parsed from the fork event's artifact bytes."""
    ref = stub_fork_ref(chain)
    if ref is None:
        raise AssertionError("the caller checked the chain has a stub fork")
    if len(data) != ref.bytes:
        why = f"the frozen stub script {ref.sha256} is {len(data)} bytes, not {ref.bytes}"
        return Err(ParseError("artifact_corrupt", why))
    try:
        return Ok(parse_stubs(_SCRIPT.validate_json(data)))
    except (ValidationError, ValueError):
        why = f"the frozen stub script {ref.sha256} is not a stub script"
        return Err(ParseError("artifact_corrupt", why))

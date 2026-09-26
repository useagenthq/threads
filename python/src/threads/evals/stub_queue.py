"""The stub entries a run answers from (spec lane 32, A.3). `stubs.json` numbers occurrences per
(tool, args_hash) across the whole recorded conversation, prefix entries first and marked
`scope: "prefix"`. Picking a scope renumbers what is left from 0, so each key stays a queue the
run consumes in log order."""

from collections.abc import Mapping
from typing import Literal

from pydantic import JsonValue

from threads.loop.stubs import Stub, StubGateway, parse_stubs

type StubScope = Literal["turn", "conversation"]
"""turn: the saved turn's entries only (offline, and a continued prefix)."""


def _scoped(script: Mapping[str, JsonValue], scope: StubScope) -> list[JsonValue]:
    stubs = script.get("stubs")
    if not isinstance(stubs, list):
        raise ValueError("a stub script needs a stubs list")
    kept: list[JsonValue] = []
    for entry in stubs:
        if not isinstance(entry, dict):
            raise ValueError("a stub is an object")
        if scope == "turn" and entry.get("scope") is not None:
            continue
        kept.append({k: v for k, v in entry.items() if k != "scope"})
    return kept


def stub_queue(script: Mapping[str, JsonValue], scope: StubScope) -> tuple[Stub, ...]:
    """The entries of `scope`, renumbered from 0 per (tool, args_hash) in their file order."""
    seen: dict[tuple[str, str], int] = {}
    parsed = parse_stubs({"stubs": _scoped(script, scope)})
    out: list[Stub] = []
    for stub in parsed:
        key = (stub.tool, stub.args_hash)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        out.append(Stub(stub.tool, stub.args_hash, occurrence, stub.output, stub.is_error))
    return tuple(out)


def stub_gateway(script: Mapping[str, JsonValue], scope: StubScope) -> StubGateway:
    """One gateway for a whole simulated conversation, so its queue spans every turn."""
    return StubGateway(stub_queue(script, scope))

"""The idle-only controls (spec/api.json `Thread.compact`, `Thread.set_output_style`): each is
appended only between turns, by the principal it names, and the next run acts on it."""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import Store
from threads.log import BranchId, ParseError, Principal
from threads.reduce import Fold
from threads.reduce.fold import policy
from threads.result import Err, Ok
from threads.store import Draft
from threads.thread.control import Controlled, actor, append, forbidden

if TYPE_CHECKING:
    from pydantic import JsonValue


async def compact(
    store: Store, branch: BranchId, principal: Principal, instructions: str | None
) -> Controlled:
    """compaction_requested: the next run summarizes everything up to it before its first
    model request, and records the outcome."""
    if principal.tenant != store.tenant:
        return forbidden("another tenant's thread")
    if instructions == "":
        return Err(ParseError("invalid_request", "instructions must be non-empty text, or omitted"))

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        if fold.compaction_request is not None:
            return Err(ParseError("invalid_transition", "a compaction is already requested"))
        if fold.first_input is None:
            return Err(ParseError("invalid_transition", "nothing to compact yet"))
        data: dict[str, JsonValue] = {} if instructions is None else {"instructions": instructions}
        return Ok((Draft("compaction_requested", data, actor("user", principal)),))

    return await append(store, branch, build, idle=True)


async def set_output_style(
    store: Store, branch: BranchId, name: str, principal: Principal
) -> Controlled:
    """The pinned style's text as a trusted instruction after the prompt prefix."""
    if principal.tenant != store.tenant:
        return forbidden("another tenant's thread")

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        pinned = policy(fold)
        styles: Mapping[str, str] = (
            {} if pinned is None or pinned.output_styles is MISSING else pinned.output_styles
        )
        text = styles.get(name)
        if text is None:
            defined = ", ".join(styles) or "none"
            return Err(
                ParseError("not_found", f"no output style {name}; the agent defines {defined}")
            )
        data: dict[str, JsonValue] = {
            "source": "output_style",
            "trust": "trusted_instruction",
            "origin": {"id": name},
            "text": text,
        }
        return Ok((Draft("injected", data, actor("user", principal)),))

    return await append(store, branch, build, idle=True)

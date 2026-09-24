# pyright: strict
"""Whether a received mail opens a turn (spec/schema/README.md, "Which events open a turn"): one
reading shared by the reference validator and the run projection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import text

if TYPE_CHECKING:
    from collections.abc import Collection

    from .jcs import JsonValue, Obj

NOTICES = frozenset({"member_settled", "member_ended"})


def mail_opens_turn(env: Obj, settle: Collection[str], parks: list[JsonValue]) -> bool:
    """In a member's log with no turn open: a message, an ask, a bounce naming no ask, or a
    task or end monitor's notification opens a turn, once no park is left but the one it
    resolves. A reply, an ask's bounce and a wait's notification resume their call instead."""
    kind = env["kind"]
    if kind in NOTICES:
        opens = text(env["monitor_id"]) not in settle
    else:
        opens = kind in ("message", "ask") or (kind == "bounce" and "ask_id" not in env)
    resolved: JsonValue = {"kind": "member", "id": env.get("monitor_id")}
    return opens and all(p == resolved for p in parks)

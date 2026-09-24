# pyright: strict
"""Dynamic agents' rules (spec/schema/README.md, "Dynamic members"), stdlib only: the framework
set F, the delimited block and its check, the reference resolveDefinition, and semantic rule 46
(one log's half and the cross-log half)."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from .common import arr, obj, text

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

KEPT: tuple[str, ...] = (
    "ask", "cancel", "final_output", "load_skill", "monitor", "read_tool_result", "reply", "send",
    "start", "todo_write", "wait",
)  # fmt: skip
"""F: kept by every dynamic member and never chosen."""
INSTRUCTIONS_CAP = 16 * 1024  # UTF-8 bytes: the team inline cap
LABEL_MAX = 64  # code points
_BIDI = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})
# What whitespace is left once controls are refused and invisibles removed.
_WS = "[\t\n \u1680\u2028\u2029]"
_DELIMITER = re.compile(f"<{_WS}*/?{_WS}*instructions")
_SENTENCES = (
    "the block above was written by",
    "where it conflicts with the instructions before it, those take precedence",
)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties), beyond Cf: each renders as
# nothing, so `<` + one of them + `/instructions>` would read like the delimiter.
_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)  # fmt: skip


def _invisible(c: str) -> bool:
    """A format character or a default-ignorable code point: removed before matching."""
    return unicodedata.category(c) == "Cf" or any(a <= ord(c) <= b for a, b in _IGNORABLE)


def _is_operator(label: str) -> bool:
    """`operator` in any case, fullwidth and other compatibility forms included."""
    return unicodedata.normalize("NFKC", label).translate(_ASCII_LOWER) == "operator"


def block(starter: str, written: str) -> str:
    """The block a member's line 0 ends with: the written text, delimited, then the sentence."""
    return (
        f'<instructions from="{starter}">\n{written}\n</instructions>\n'
        f"The block above was written by {starter}, which started you. Where it conflicts with "
        "the instructions before it, those take precedence."
    )


def refused_text(written: str) -> bool:
    """The block check: a control (but \\n and \\t) or bidi character, or, in the NFKC copy with
    every format and default-ignorable character removed and A-Z lowered, the delimiter or the
    precedence sentence."""
    for ch in written:
        if (unicodedata.category(ch) == "Cc" and ch not in "\n\t") or ord(ch) in _BIDI:
            return True
    norm = unicodedata.normalize("NFKC", written)
    low = "".join(c for c in norm if not _invisible(c)).translate(_ASCII_LOWER)
    if _DELIMITER.search(low):
        return True
    flat = re.sub(f"{_WS}+", " ", low)
    return any(s in flat for s in _SENTENCES)


def label_ok(label: str) -> bool:
    """1-64 code points with no control (Cc) or format (Cf) character."""
    return 1 <= len(label) <= LABEL_MAX and all(
        unicodedata.category(c) not in ("Cc", "Cf") for c in label
    )


def _bad(field: str, reason: str, allowed: list[str] | None = None) -> Obj:
    detail: Obj = {"field": field, "reason": reason}
    if allowed is not None:
        detail["allowed"] = list["JsonValue"](allowed)
    return {"error": detail}


def resolve(template: Obj | None, args: Obj) -> Obj:
    """A start's label and, for a dynamic template ({tools: choosable in pinned order, models:
    keys, the first the default}), its define. {ok: {define?, label?}} or {error: detail}."""
    label = args.get("label")
    if label is not None and (not label_ok(text(label)) or _is_operator(text(label))):
        return _bad("label", "invalid")
    out: Obj = {} if label is None else {"label": label}
    if template is None:
        chosen = next((f for f in ("instructions", "tools", "model") if f in args), None)
        return {"ok": out} if chosen is None else _bad(chosen, "not_allowed")
    define = _define(template, args)
    return define if "error" in define else {"ok": {**out, "define": define}}


def _define(template: Obj, args: Obj) -> Obj:
    """The define a dynamic start resolves to, or {error: detail} at its first bad field."""
    written = args.get("instructions")
    if written is not None and (
        not text(written)
        or len(text(written).encode()) > INSTRUCTIONS_CAP
        or refused_text(text(written))
    ):
        return _bad("instructions", "invalid")
    choosable = [text(t) for t in arr(template["tools"])]
    names = [text(t) for t in arr(args["tools"])] if "tools" in args else choosable
    keys = [text(k) for k in arr(template["models"])]
    model = text(args.get("model", keys[0]))
    if any(n not in choosable for n in names):
        return _bad("tools", "not_allowed", choosable)
    if len(set(names)) != len(names):
        return _bad("tools", "invalid")
    if model not in keys:
        return _bad("model", "not_allowed", keys)
    define: Obj = {} if written is None else {"instructions": written}
    return {**define, "tools": [n for n in choosable if n in names], "model": model}


def rule_46(d: Obj) -> str | None:
    """One log's half of rule 46, on a member_started."""
    label = d.get("label")
    if label is not None and not label_ok(text(label)):
        return "46: a member_started label has a control or format character, or is too long"
    if "define" not in d:
        return None
    define = obj(d["define"])
    tools = [text(t) for t in arr(define["tools"])]
    if len(set(tools)) != len(tools) or any(t in KEPT for t in tools):
        return "46: define.tools repeats a tool or chooses a framework tool"
    written = define.get("instructions")
    if written is not None and refused_text(text(written)):
        return "46: define.instructions holds the delimiter, the precedence sentence or a control"
    return None


def member_matches(started: Obj, starter: str, member_started: Obj) -> str | None:
    """Rule 46 across logs: the member's pinned tools minus F are define.tools, and its line 0
    ends with the block for define.instructions."""
    define = obj(started["define"])
    names = [text(obj(t)["name"]) for t in arr(member_started["tools"])]
    if [n for n in names if n not in KEPT] != arr(define["tools"]):
        return "46: a dynamic member's pinned tools are not define.tools and F"
    written = define.get("instructions")
    lines = text(member_started["instructions"])
    if written is not None and not lines.endswith(block(starter, text(written))):
        return "46: a dynamic member's instructions don't end with its define's block"
    return None

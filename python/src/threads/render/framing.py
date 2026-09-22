"""Render v1 text framing: the wrappers around stored text and the fixed strings.

Every wrapper escapes its attribute values and its whole body, so stored text (a memory id, a
memory body, a summary, a call id) can never close a wrapper or open another one. Only the
wrapper's own tags are raw (spec/schema/README.md, "Framing escape").
"""

from collections.abc import Sequence

from pydantic import JsonValue

CLEARED = "[tool result cleared: call_id={}; read it with read_tool_result]"
REDACTED = b"[redacted]"
COMPACT_INSTRUCTION = (
    "Summarize the conversation so far for your own continuation. Keep the user's goals and "
    "constraints, decisions made, files and identifiers touched, open tasks with their status, "
    "and the next step. Reply with the summary only."
)
GUIDE_PREFIX = "\n\nAdditional instructions:\n"

# `&` first, so the entities the later replacements insert are never escaped twice.
_ENTITIES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;"))


def esc(text: str) -> str:
    for raw, entity in _ENTITIES:
        text = text.replace(raw, entity)
    return text


def reference(source: str, ident: str, body: str) -> str:
    return (
        f'<reference source="{esc(source)}" id="{esc(ident)}" untrusted="true">\n'
        f"{esc(body)}\n</reference>"
    )


def context(source: str, ident: str, body: str) -> str:
    return f'<context source="{esc(source)}" id="{esc(ident)}">\n{esc(body)}\n</context>'


def heartbeat(call_ids: Sequence[str]) -> str:
    return "<heartbeat>\nrunning: " + ", ".join(esc(c) for c in call_ids) + "\n</heartbeat>"


def user_line(text: str) -> JsonValue:
    return {"role": "user", "content": [{"type": "text", "text": text}]}

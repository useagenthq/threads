"""fold_ag_ui: the messages @ag-ui/client@1.0.0's defaultApplyEvents builds from a stream,
ported for the events threads sends (spec/schema/ui/README.md, "Replays and the snapshot"). A
replay's MESSAGES_SNAPSHOT is this fold of the canonical frames, so it equals what an
uninterrupted stock client holds by construction. The shared vectors pin it to that version."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from threads.host.ui.frame import Chunk

type Message = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class UserTurn:
    id: str
    text: str
    frames: Sequence[Chunk]


def fold_ag_ui(turns: Sequence[UserTurn]) -> list[Message]:
    """The stock client's messages after each turn's user message and its frames, in order."""
    fold = AgUiFold()
    for turn in turns:
        fold.messages.append({"id": turn.id, "role": "user", "content": turn.text})
        for chunk in turn.frames:
            fold.apply(chunk)
    return fold.messages


def _text(e: Chunk, key: str) -> str:
    value = e.get(key)
    return value if isinstance(value, str) else ""


def _calls(m: Message) -> list[JsonValue]:
    calls = m.get("toolCalls")
    return calls if isinstance(calls, list) else []


def _call_id(call: JsonValue) -> str | None:
    value = call.get("id") if isinstance(call, dict) else None
    return value if isinstance(value, str) else None


def _id(m: Message) -> str:
    value = m.get("id")
    return value if isinstance(value, str) else ""


class AgUiFold:
    def __init__(self, messages: Sequence[Mapping[str, JsonValue]] = ()) -> None:
        self.messages: list[Message] = [dict(m) for m in messages]

    def apply(self, e: Chunk) -> None:
        match e.get("type"):
            case "TEXT_MESSAGE_START":
                self._open(_text(e, "messageId"), _text(e, "role") or "assistant")
            case "REASONING_MESSAGE_START":
                self._open(_text(e, "messageId"), "reasoning")
            case "TEXT_MESSAGE_CONTENT" | "REASONING_MESSAGE_CONTENT":
                self._append(_text(e, "messageId"), _text(e, "delta"))
            case "TOOL_CALL_START":
                self._call(e)
            case "TOOL_CALL_ARGS":
                self._args(_text(e, "toolCallId"), _text(e, "delta"))
            case "TOOL_CALL_RESULT":
                self._result(e)
            case "MESSAGES_SNAPSHOT":
                self._merge(e.get("messages"))
            case _:
                pass

    def _find(self, message_id: str) -> Message | None:
        return next((m for m in self.messages if m.get("id") == message_id), None)

    def _open(self, message_id: str, role: str) -> None:
        if self._find(message_id) is None:
            self.messages.append({"id": message_id, "role": role, "content": ""})

    def _append(self, message_id: str, delta: str) -> None:
        target = self._find(message_id)
        if target is not None:
            content = target.get("content")
            target["content"] = f"{content if isinstance(content, str) else ''}{delta}"

    def _owner(self, call_id: str) -> Message | None:
        return next(
            (m for m in self.messages if any(_call_id(c) == call_id for c in _calls(m))), None
        )

    def _call(self, e: Chunk) -> None:
        call_id = _text(e, "toolCallId")
        if self._owner(call_id) is not None:
            return
        parent = e.get("parentMessageId")
        existing = self._find(parent) if isinstance(parent, str) else None
        owner = existing if existing is not None and existing.get("role") == "assistant" else None
        if owner is None:
            owner_id = parent if isinstance(parent, str) and existing is None else call_id
            created: Message = {"id": owner_id, "role": "assistant", "toolCalls": []}
            self.messages.append(created)
            owner = created
        calls = _calls(owner)
        function: JsonValue = {"name": _text(e, "toolCallName"), "arguments": ""}
        calls.append({"id": call_id, "type": "function", "function": function})
        owner["toolCalls"] = calls

    def _args(self, call_id: str, delta: str) -> None:
        owner = self._owner(call_id)
        for call in [] if owner is None else _calls(owner):
            function = call.get("function") if isinstance(call, dict) else None
            if _call_id(call) == call_id and isinstance(function, dict):
                args = function.get("arguments")
                function["arguments"] = f"{args if isinstance(args, str) else ''}{delta}"
                return

    def _result(self, e: Chunk) -> None:
        """A result goes right after its call's message and any results already there."""
        call_id = _text(e, "toolCallId")
        message: Message = {
            "id": _text(e, "messageId"),
            "role": "tool",
            "toolCallId": call_id,
            "content": _text(e, "content"),
        }
        owner = next(
            (
                i
                for i, m in enumerate(self.messages)
                if m.get("role") == "assistant" and any(_call_id(c) == call_id for c in _calls(m))
            ),
            None,
        )
        if owner is None:
            self.messages.append(message)
            return
        at = owner + 1
        while at < len(self.messages) and self.messages[at].get("role") == "tool":
            at += 1
        self.messages.insert(at, message)

    def _merge(self, raw: JsonValue) -> None:
        """A merge by id: kept in place when named, dropped when not, appended when new."""
        incoming = [dict(m) for m in (raw if isinstance(raw, list) else []) if isinstance(m, dict)]
        by_id = {_id(m): m for m in incoming}
        keeps_reasoning = not any(m.get("role") == "reasoning" for m in incoming)
        kept = [
            by_id.get(_id(m), m)
            for m in self.messages
            if _id(m) in by_id or (keeps_reasoning and m.get("role") == "reasoning")
        ]
        ids = {_id(m) for m in kept}
        self.messages = [*kept, *(m for m in incoming if _id(m) not in ids)]

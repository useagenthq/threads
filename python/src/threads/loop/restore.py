"""L3 restore: what the dropped range held that the agent still needs comes back as appended
events, in the same batch as `compacted` and in a fixed order: the output style when the range
holds the latest one, skills, recently touched files, the todo list, a heartbeat of running work.
The after_compact and session_start{compact} hook injections follow in a later batch. Files are
read from the sandbox as a framework read_only operation; everything restored but the style
renders as untrusted reference."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING

from threads.hooks.runner import TEXTS, decision_draft, injected
from threads.log import (
    Event,
    InjectedEvent,
    TodosUpdatedEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolResultLateEvent,
)
from threads.log.digest import sha256_hex
from threads.loop import defaults
from threads.loop.drafts import draft
from threads.loop.gates import texts
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store import Draft

if TYPE_CHECKING:
    from pydantic import JsonValue

    from threads.hooks.types import HookName

_FILE_TOOLS = frozenset({"read", "write", "edit"})
BYTES_PER_TOKEN = 4


async def restore_drafts(rt: Runtime, dropped: Sequence[Event]) -> list[Draft]:
    """What a compaction of `dropped` restores, built before `compacted` so both land in one
    batch: a crash never leaves a summary without its restore."""
    limits = defaults.context(rt.fold).restore
    return [
        *_style(rt.events, dropped),
        *_skills(dropped, limits.skill_tokens, limits.skills_total_tokens),
        *await _files(rt, dropped, limits.max_files, limits.file_tokens),
        *_todos(rt.events),
        *_heartbeat(rt.events),
    ]


async def restore_hooks(rt: Runtime) -> Halt | None:
    """The context hooks after a compaction's batch, their injections in one append."""
    drafts: list[Draft] = []
    failed = False
    points: tuple[tuple[HookName, tuple[object, ...]], ...] = (
        ("after_compact", (rt.writer.state(),)),
        ("session_start", ("compact",)),
    )
    for hook, args in points:
        if not rt.hooks.has(hook):
            continue
        ran = await rt.hooks.run(hook, TEXTS, *args)
        drafts += [decision_draft(hook, r, "proceed") for r in ran]
        failed = failed or any(r.failure is not None for r in ran)
        drafts += [d for r in ran if r.failure is None for d in injected(r.extension, texts(r))]
    if failed:
        # A context hook's failure denies the step that waits for it.
        drafts.append(draft("turn_completed", {"reason": "error"}))
    if not drafts:
        return None
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else None


def _style(events: Sequence[Event], dropped: Sequence[Event]) -> list[Draft]:
    """The latest output style, when the range drops it: the thread keeps replying in it."""
    latest = next(
        (e for e in reversed(events) if isinstance(e, InjectedEvent) and _is_style(e)), None
    )
    if latest is None or not dropped or not dropped[0].seq <= latest.seq <= dropped[-1].seq:
        return []
    data = to_json(latest.data)
    return [draft("injected", data)] if isinstance(data, dict) else []


def _is_style(event: InjectedEvent) -> bool:
    return event.data.source == "output_style"


def _skills(dropped: Sequence[Event], each_tokens: int, total_tokens: int) -> list[Draft]:
    """Skills loaded in the dropped range, re-appended with the same origin, capped."""
    out: list[Draft] = []
    budget = total_tokens * BYTES_PER_TOKEN
    for event in dropped:
        if not isinstance(event, InjectedEvent) or event.data.source != "skill":
            continue
        size = len(event.data.text.encode()) if event.data.text is not MISSING else 0
        if size > each_tokens * BYTES_PER_TOKEN or size > budget:
            continue
        budget -= size
        data = to_json(event.data)
        if isinstance(data, dict):
            out.append(draft("injected", data))
    return out


async def _files(
    rt: Runtime, dropped: Sequence[Event], max_files: int, file_tokens: int
) -> list[Draft]:
    """The files tools touched in the range, newest first: their content with its hash, or a
    path-only note when it can't come back whole."""
    paths: list[str] = []
    for event in reversed(dropped):
        if not isinstance(event, ToolCallEvent) or event.data.name not in _FILE_TOOLS:
            continue
        path = event.data.input.get("path")
        if isinstance(path, str) and path not in paths:
            paths.append(path)
    out: list[Draft] = []
    for path in paths[:max_files]:
        data = None if rt.read_file is None else await rt.read_file(path)
        origin: dict[str, JsonValue] = {"id": path}
        text = f"[{path} was not restored; read it again if needed]"
        if data is not None and len(data) <= file_tokens * BYTES_PER_TOKEN:
            try:
                text = data.decode("utf-8")
                origin["version"] = sha256_hex(data)
            except UnicodeDecodeError:
                pass
        body = {"source": "attachment", "trust": "untrusted_reference", "origin": origin}
        out.append(draft("injected", {**body, "text": text}))
    return out


def _todos(events: Sequence[Event]) -> list[Draft]:
    latest = next((e for e in reversed(events) if isinstance(e, TodosUpdatedEvent)), None)
    if latest is None or not latest.data.todos:
        return []
    text = "\n".join(f"- [{t.status}] {t.content}" for t in latest.data.todos)
    origin: JsonValue = {"id": "todos"}
    return [
        draft(
            "injected",
            {"source": "todo", "trust": "untrusted_reference", "origin": origin, "text": text},
        )
    ]


def _heartbeat(events: Sequence[Event]) -> list[Draft]:
    """Deferred calls whose late result hasn't arrived. ponytail: running children are not
    listed yet; add them with background subagents."""
    late = {e.data.call_id for e in events if isinstance(e, ToolResultLateEvent)}
    running: list[JsonValue] = [
        e.data.call_id
        for e in events
        if isinstance(e, ToolResultEvent)
        and e.data.origin == "deferred"
        and e.data.call_id not in late
    ]
    return [draft("heartbeat", {"running_call_ids": running})] if running else []

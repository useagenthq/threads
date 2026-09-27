"""The `remote_task_state` events an exchange observed.

Each is what we SAW, never what we decided, which is why they are not critical: a reader that
skips them reduces the log to the same state. They are appended after the call's `effect_commit`,
because a partner's task state is only observed for a call we hold a receipt for (rule 57)."""

from collections.abc import Collection, Sequence

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threadsai._generated.a2a_v1 import Task
from threadsai.log import ArtifactRef
from threadsai.log.jcs import canonicalize
from threadsai.loop.tools import Invocation
from threadsai.result import Ok
from threadsai.store import Draft


async def state_drafts(
    call: Invocation, call_id: str, seen: Sequence[Task], already: Collection[str]
) -> tuple[Draft, ...]:
    """One draft per state change, deduplicated by state **and** content hash: a repeat of the same
    state with the same bytes is not appended twice, within this exchange or against what earlier
    attempts of the same call already recorded."""
    drafts: list[Draft] = []
    kept = set(already)
    for task in seen:
        status = await _stored(call, task.status.model_dump(mode="json"))
        key = f"{task.status.state}:{'' if status is None else status.sha256}"
        if key in kept:
            continue
        kept.add(key)
        artifacts = (
            None
            if task.artifacts is MISSING or not task.artifacts
            else await _stored(call, [a.model_dump(mode="json") for a in task.artifacts])
        )
        data: dict[str, JsonValue] = {
            "call_id": call_id,
            "task_id": task.id,
            "state": task.status.state,
        }
        if status is not None:
            data["status_ref"] = status.model_dump(mode="json")
        if artifacts is not None:
            data["artifacts_ref"] = artifacts.model_dump(mode="json")
        drafts.append(Draft("remote_task_state", data, {"kind": "host"}, False))
    return tuple(drafts)


async def _stored(call: Invocation, value: JsonValue) -> ArtifactRef | None:
    """The observation's bytes as canonical JSON, so the same observation hashes the same
    everywhere."""
    text = canonicalize(value)
    return await call.put(text.value, "application/json") if isinstance(text, Ok) else None

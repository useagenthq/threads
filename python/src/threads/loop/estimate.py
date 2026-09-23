"""Token accounting for the context ladder. Always an estimate, never a count or
a bound: the input side of the last turn response with known usage in this settings epoch, plus
`ceil(bytes / 4)` of what the next request adds since that request; with no known usage,
`ceil(request bytes / 4)`."""

import math
from collections.abc import Sequence

from threads.log import (
    Event,
    ModelRequestEvent,
    ModelResponseEvent,
    SettingsChangedEvent,
    ThreadStartedEvent,
    Usage,
)
from threads.loop import defaults
from threads.reduce.fold import Fold

BYTES_PER_TOKEN = 4


def window(fold: Fold) -> int:
    """W: the context window less `reserve_tokens`."""
    return defaults.effective_window(fold)


def estimate(events: Sequence[Event], next_request_bytes: int) -> int:
    requests = {
        e.event_id: e
        for e in events
        if isinstance(e, ModelRequestEvent) and e.data.purpose != "compaction"
    }
    for event in reversed(events):
        if isinstance(event, SettingsChangedEvent | ThreadStartedEvent):
            break
        if not isinstance(event, ModelResponseEvent):
            continue
        request = requests.get(event.data.request_event_id)
        known = _input_side(event.data.usage)
        if request is not None and known is not None:
            added = max(0, next_request_bytes - request.data.request_ref.bytes)
            return known + math.ceil(added / BYTES_PER_TOKEN)
    return math.ceil(next_request_bytes / BYTES_PER_TOKEN)


def _input_side(usage: Usage) -> int | None:
    if usage.input_tokens is None:
        return None
    total = usage.input_tokens
    for cached in (usage.cache_read_tokens, usage.cache_write_tokens):
        if isinstance(cached, int):
            total += cached
    return total

"""Span and trace ids derived from the log (spec/otel/README.md, "Ids"): a re-send carries the
same ids, and both implementations derive the same ones."""

import hashlib
import re


def nonzero(hex_id: str) -> str:
    """An all-zero id is invalid in OpenTelemetry: its last byte becomes 1."""
    return f"{hex_id[:-2]}01" if re.fullmatch(r"0+", hex_id) else hex_id


def _digest(tag: str, *parts: str) -> str:
    return hashlib.sha256("\0".join((f"threads-{tag}", *parts)).encode()).hexdigest()


def span_id(branch_id: str, event_id: str, call_id: str | None = None) -> str:
    """The span a log event opens; a continuation also names its call."""
    parts = (branch_id, event_id) if call_id is None else (branch_id, event_id, call_id)
    return nonzero(_digest("span", *parts)[:16])


def trace_id(thread_id: str, event_id: str) -> str:
    """The trace rooted at an event of a thread."""
    return nonzero(_digest("trace", thread_id, event_id)[:32])


def loss_ids(observer: str, thread_id: str, deleted_at: int) -> tuple[str, str]:
    """(trace id, span id) of a threads.export.possibly_lost span."""
    return (
        nonzero(_digest("loss", observer, thread_id)[:32]),
        nonzero(_digest("loss", observer, thread_id, str(deleted_at))[:16]),
    )

"""The pinned policy sections the loop reads, with the ADR defaults when a section is absent
(`Policy`: each section is absent or complete)."""

from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    ClearResults,
    Compact,
    Context,
    ModelSettings,
    Restore,
    Retry,
    Spill,
    Threshold,
    Threshold1,
    Threshold2,
)
from threads.reduce.fold import Fold, policy

RETRY: Final = Retry(
    max_retries=8,
    base_delay_ms=1000,
    max_delay_ms=32_000,
    max_retry_after_ms=60_000,
    max_total_wait_ms=600_000,
    crash_resends=2,
    fallback_after=3,
    fallback_scope="turn",
    heartbeat_ms=15_000,
)
"""."""

CONTEXT: Final = Context(
    reserve_tokens=20_000,
    cache_ttl_ms=300_000,
    clear_results=ClearResults(trigger=Threshold2(permille=700), keep_recent=5, exclude_tools=[]),
    spill=Spill(
        threshold_bytes=32_768, head_bytes=2048, tail_bytes=1024, request_budget_bytes=204_800
    ),
    compact=Compact(
        trigger=Threshold2(permille=850), keep_tail=Threshold1(tokens=20_000), max_failures=3
    ),
    restore=Restore(max_files=5, file_tokens=5000, skill_tokens=5000, skills_total_tokens=25_000),
    max_output_continuations=3,
    defer_tools="auto",
    defer_threshold=Threshold2(permille=100),
    server_edits="disabled",
)
"""."""

WINDOW: Final = 200_000
"""The context window assumed when the policy lists no models."""


def retry(fold: Fold) -> Retry:
    pinned = policy(fold)
    return RETRY if pinned is None or pinned.retry is MISSING else pinned.retry


def context(fold: Fold) -> Context:
    pinned = policy(fold)
    return CONTEXT if pinned is None or pinned.context is MISSING else pinned.context


def max_pauses(fold: Fold) -> int:
    """pause_turn re-requests per turn; absent means 3 (spec/schema/README.md, turn endings)."""
    cap = context(fold).max_pause_continuations
    return 3 if cap is MISSING else cap


def fallbacks(fold: Fold) -> tuple[ModelSettings, ...]:
    pinned = policy(fold)
    return () if pinned is None or pinned.fallback is MISSING else tuple(pinned.fallback)


def effective_window(fold: Fold) -> int:
    """W: the context window less `reserve_tokens`. ponytail: the primary
    model's window; a fallback's own window when settings_changed names one."""
    pinned = policy(fold)
    window = WINDOW
    if pinned is not None and pinned.models is not MISSING:
        window = pinned.models[0].context_window
    return window - context(fold).reserve_tokens


def tokens(threshold: Threshold, fold: Fold) -> int:
    """A threshold in tokens: permille of the effective window W."""
    if isinstance(threshold, Threshold1):
        return threshold.tokens
    return threshold.permille * effective_window(fold) // 1000

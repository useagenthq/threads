# pyright: strict
"""Output styles (Thread.setOutputStyle): the pinned text as a trusted instruction after line 0,
restored after a compaction, and semantic rule 29."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .pieces import answer, reject, render_case, user
from .thread_methods import CONCISE, FAM, STYLES, base, compacted, style, summarize

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    _style_rendered(root)
    _style_rejections(root)
    _style_restored(root)


def _style_rendered(root: pathlib.Path) -> None:
    log = base(output_styles=STYLES)
    style(log, "concise", CONCISE)
    user(log, "Next?")
    answer(log, "Done.")
    user(log, "And now?")
    render_case(
        root,
        (
            "output-style-injected-rendered",
            FAM,
            "Thread.setOutputStyle appends the pinned style text as injected{source: "
            "output_style, trust: trusted_instruction}. It renders as a <context> user line "
            "after line 0, and line 0 is byte-equal before and after it (C7 within one epoch).",
        ),
        log,
    )


def _style_rejections(root: pathlib.Path) -> None:
    pinned = CONCISE
    for name, desc, injected in (
        (
            "output-style-text-mismatch-rejected",
            "A text other than the pinned one.",
            ("concise", "Shout.", "user"),
        ),
        ("output-style-unknown-name-rejected", "A name the pin lacks.", ("loud", pinned, "user")),
        (
            "output-style-by-model-rejected",
            "A style the model injected.",
            ("concise", pinned, "model"),
        ),
    ):
        log = base(output_styles=STYLES)
        style(log, *injected)
        reject(root, (name, "log", desc + " Semantic rule 29: invalid_transition."), log)


def _style_restored(root: pathlib.Path) -> None:
    log = base(output_styles=STYLES)
    first = log.events[1]
    style(log, "concise", CONCISE)
    user(log, "Next?")
    answer(log, "Done.")
    last = log.events[-1]
    user(log, "Continue.")
    compacted(log, (first, last), summarize(log))
    style(log, "concise", CONCISE, actor="host")
    render_case(
        root,
        (
            "output-style-restored-after-compaction",
            FAM,
            "A compaction drops the range holding the latest output style; the restore "
            "re-appends it with actor host in the same batch as compacted, so the next request "
            "still carries the style after the summary.",
        ),
        log,
    )

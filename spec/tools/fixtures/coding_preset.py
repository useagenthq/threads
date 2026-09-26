# pyright: strict
"""`coding-preset.json`: what `codingAgent()` pins when the caller passes nothing — the
instructions (spec/api.json `constants.CODING_INSTRUCTIONS`), the model and the permissions.

They are here, not in either package, because both packages must pin the same values: line 0 is
byte-equal on every request of a settings epoch (framework invariant 5), and a thread started by
the TypeScript preset has to reduce to the same state as one started by the Python preset. Each
suite asserts its preset's own pin against this file, so a reworded sentence or a renamed mode is
a one-line spec diff a reviewer sees, not a silent drift between the two.

The text is what a coding agent needs and nothing more: explore first, plan with todo_write, the
smallest change, verify it, memory only when asked. The baseline commit and the closing
`git diff` are how the user gets the edits out of the sandbox, since /workspace is a copy and is
never written back.

`--renormalize` is not decoration. A file the host uploads into a sandbox lands with mtime 0, so
two versions of the same length are indistinguishable to git's stat cache and a plain
`git add -A` can leave an edit like `x = 1` -> `x = 2` out of the diff entirely (observed against
a real daemon, and it depends on whether the daemon's untar reuses the inode). `--renormalize`
re-reads every tracked file's contents instead of trusting the cache. It covers only tracked
files, which is why the plain `git add -A` in front of it stays: that is what stages the new and
deleted ones."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import CASES
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import JsonValue

VECTOR = CASES.parent / "vectors" / "coding-preset.json"

INSTRUCTIONS = (
    "You are a software engineer working in a sandbox. /workspace is a copy of the user's files; "
    "your edits stay in the sandbox. Before your first edit, record a baseline in /workspace: "
    "`git init -q; git add -A && git -c user.name=threads -c user.email=threads@localhost commit "
    "-q --no-verify --allow-empty -m threads-baseline && git tag -f threads-baseline`. Explore "
    "before you change anything: read the relevant files and run the existing tests. Plan "
    "multi-step work with todo_write and keep it current. Make the smallest change that solves "
    "the task, then verify it by running the tests or the program. Save to memory only when the "
    "user asks you to. End your answer with what you changed, what you verified and anything you "
    "could not do, followed by the output of `git add -A && git add -A --renormalize && git "
    "diff --cached --binary threads-baseline`."
)


MODEL = "claude-sonnet-5"
"""The Anthropic model the preset asks for; its key is resolved on the host, never in the box."""

PERMISSIONS: dict[str, JsonValue] = {"mode": "accept_edits", "allow": ["bash(*)"]}
"""The rest of the policy (the protected paths, allow_bypass) stays agent()'s own default."""


def _vector() -> str:
    return dump({"instructions": INSTRUCTIONS, "model": MODEL, "permissions": PERMISSIONS})


def write() -> None:
    VECTOR.write_text(_vector())


def check() -> list[str]:
    if not VECTOR.is_file() or VECTOR.read_text() != _vector():
        return [f"{VECTOR.name} is stale; run gen_fixtures.py"]
    return []

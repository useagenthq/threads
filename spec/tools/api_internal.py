# pyright: strict
"""Internal entries stay internal: no docs page, README or example names them.

`@threads/core/internal/*` (TypeScript subpaths) and `threads.store._feed` (Python) are the
readers adapter packages build on. They are not in spec/api.json, not documented, and may change
in any release, so a mention in user-facing text is an error. Stdlib only.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib

INTERNAL = re.compile(r"@threads/core/internal/[\w/-]*|threads\.store\._feed")


def user_facing(root: pathlib.Path) -> list[pathlib.Path]:
    """The docs pages, READMEs and examples a user reads."""
    return sorted(
        {
            *(root / "docs" / "content").rglob("*.mdx"),
            *root.glob("README.md"),
            *root.glob("typescript/packages/*/README.md"),
            *root.glob("typescript/packages/*/examples/**/*.ts"),
            *root.glob("python/examples/**/*.py"),
        }
    )


def check_internal_mentions(root: pathlib.Path) -> list[str]:
    return [
        f"{path.relative_to(root)}: mentions {found}, an internal entry that is never documented"
        for path in user_facing(root)
        for found in INTERNAL.findall(path.read_text())
    ]

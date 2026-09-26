"""The workspace deny-list (spec/schema/README.md, Workspace inputs, rules 1 and 3), matched per
path segment: exact key names only, so `id_utils/` and `id_token.ts` are kept."""

from collections.abc import Sequence
from typing import Final

_EXACT: Final = frozenset(
    {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_ecdsa_sk",
        "id_ed25519_sk",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".aws",
        ".ssh",
    }
)


def denied(parts: Sequence[str], i: int) -> bool:
    """Segment `i` of a path's `parts` is deny-listed (rule 3)."""
    name = parts[i]
    return (
        name in _EXACT
        or name.startswith(".env")
        or name.endswith((".pem", ".key"))
        or (name == "config.json" and i > 0 and parts[i - 1] == ".docker")
    )


def excluded(path: str, start: int = 0) -> bool:
    """Rules 1 and 3 on the segments of `path` from `start`: a `.git` or deny-listed segment."""
    parts = path.split("/")
    return any(p == ".git" or denied(parts, i) for i, p in enumerate(parts) if i >= start)

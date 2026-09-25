# pyright: strict
"""Workspace inputs (spec/schema/README.md, "Workspace inputs"): the deny-list vector
(vectors/workspace-exclude.json) and the reference pin of a workspace whose local_dir is outside
any git work tree, computed from the rules: `.git` and deny-listed segments skipped, `include`
re-admitting, the top-most skipped paths listed, directories 0o755, files 0o755 when executable
else 0o644."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import CASES, arr, obj, sha, text
from .jcs import JsonValue, canonical
from .pieces import dump
from .tar_vectors import manifest_hash

if TYPE_CHECKING:
    from .jcs import Obj

VECTOR = CASES.parent / "vectors" / "workspace-exclude.json"
EXACT = frozenset(
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


def denied(parts: list[str], i: int) -> bool:
    """Segment i of a path is on the deny-list (rule 3)."""
    name = parts[i]
    return (
        name in EXACT
        or name.startswith(".env")
        or name.endswith((".pem", ".key"))
        or (name == "config.json" and i > 0 and parts[i - 1] == ".docker")
    )


def excluded(path: str, start: int = 0) -> bool:
    """Rules 1 and 3 on the segments from `start`: a .git or deny-listed segment."""
    parts = path.split("/")
    return any(parts[i] == ".git" or denied(parts, i) for i in range(start, len(parts)))


_CASES: tuple[str, ...] = (
    ".env",
    ".env.test",
    ".envrc",
    "src/.env.example",
    "environment.ts",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_ecdsa_sk",
    "id_ed25519_sk",
    "keys/id_ed25519",
    "id_rsa.pub",
    "id_utils/index.ts",
    "id_token.ts",
    "certs/server.pem",
    "tls.key",
    "keyboard.ts",
    "a.keys",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".aws/credentials",
    ".ssh/config",
    "home/.ssh/known_hosts",
    ".docker/config.json",
    ".docker/daemon.json",
    "config.json",
    "app/.docker/config.json",
    ".git/config",
    "vendor/lib/.git",
    ".gitignore",
    ".github/workflows/ci.yml",
    "src/main.ts",
)


def _utf16(p: str) -> bytes:
    return p.encode("utf-16-be")


def _parents(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _admitted(path: str, include: list[str]) -> bool:
    if not excluded(path):
        return True
    for i in include:
        if path == i:
            return True
        if path.startswith(i + "/") and not excluded(path, i.count("/") + 1):
            return True
    return False


def local_dir_pin(path: str, contents: list[JsonValue], include: list[str]) -> tuple[Obj, list[Obj]]:
    """The local_dir source and its tree entries. `contents`: {path, text[, exec]}, {path,
    symlink} or {path, dir: true}; parent directories are implied."""
    kinds: dict[str, Obj] = {}
    for c in map(obj, contents):
        p = text(c["path"])
        for d in _parents(p):
            kinds.setdefault(d, {"dir": True})
        kinds[p] = c
    kept = {p for p in kinds if _admitted(p, include)}
    kept |= {d for p in kept for d in _parents(p)}
    skipped = [
        p for p in kinds if p not in kept and all(d in kept for d in _parents(p))
    ]
    entries: list[Obj] = []
    for p in kept:
        c = kinds[p]
        if "symlink" in c:
            entries.append({"path": p, "kind": "symlink", "target": c["symlink"]})
        elif "text" in c:
            data = text(c["text"]).encode()
            mode = 0o755 if c.get("exec") is True else 0o644
            entries.append(
                {"path": p, "kind": "file", "mode": mode, "size": len(data), "sha256": sha(data)}
            )
        else:
            entries.append({"path": p, "kind": "dir", "mode": 0o755})
    source: Obj = {
        "kind": "local_dir",
        "path": path,
        "skipped": list[JsonValue](sorted(skipped, key=_utf16)),
    }
    return source, entries


def files_entries(files: Obj) -> list[Obj]:
    out: list[Obj] = []
    for p, value in files.items():
        data = text(value).encode()
        out.append({"path": p, "kind": "file", "mode": 0o644, "size": len(data), "sha256": sha(data)})
    return out


def workspace_pin(workspace: Obj, contents: list[JsonValue]) -> Obj:
    """policy.workspace for `files` and a `local_dir` outside any work tree."""
    sources: list[JsonValue] = []
    entries: list[Obj] = []
    if "files" in workspace:
        sources.append({"kind": "files"})
        entries += files_entries(obj(workspace["files"]))
    if "local_dir" in workspace:
        include = [text(i) for i in arr(workspace.get("include", []))]
        source, found = local_dir_pin(text(workspace["local_dir"]), contents, include)
        sources.append(source)
        entries += found
    ordered = sorted(entries, key=lambda e: _utf16(text(e["path"])))
    tree = canonical({"tree_version": 1, "entries": list[JsonValue](ordered)})
    return {
        "tree": {"sha256": sha(tree), "bytes": len(tree)},
        "manifest_hash": manifest_hash(ordered),
        "sources": sources,
    }


def _vector() -> str:
    doc: Obj = {
        "description": (
            "The workspace deny-list with .git (spec/schema/README.md, Workspace inputs, rules 1 "
            "and 3): `excluded` is whether a local_dir or git path is skipped by its segments "
            "alone, before gitignore and include."
        ),
        "cases": [{"path": p, "excluded": excluded(p)} for p in _CASES],
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if current == _vector() else [f"{VECTOR.name}: differs; run gen_fixtures.py"]

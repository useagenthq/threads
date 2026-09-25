"""The tree artifact (spec/schema/tree.v1.schema.json): one sandbox file tree as RFC 8785 JSON,
each file's content its own artifact. The model is generated from the Zod export."""

from collections.abc import Sequence

from pydantic import JsonValue, ValidationError

from threads._generated.tree_v1 import Tree, TreeDir, TreeEntry, TreeFile, TreeSymlink
from threads.log import ParseError
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize, utf16_key
from threads.log.strict_json import parse_json
from threads.result import Err, Ok
from threads.sandbox.manifest import ManifestEntry, manifest_hash
from threads.sandbox.tree.paths import PathSet, in_root, is_normal

__all__ = [
    "PERMISSIONS",
    "Tree",
    "TreeDir",
    "TreeEntry",
    "TreeFile",
    "TreeSymlink",
    "broken",
    "encode_tree",
    "masked",
    "parse_tree",
    "sorted_tree",
    "tree_manifest_hash",
]


def sorted_tree(entries: Sequence[TreeEntry]) -> Tree:
    """A tree of `entries`, sorted by path in UTF-16 code units."""
    return Tree(tree_version=1, entries=sorted(entries, key=lambda e: utf16_key(e.path)))


def encode_tree(tree: Tree) -> bytes:
    """The tree's artifact bytes: RFC 8785 canonical JSON."""
    value: JsonValue = tree.model_dump(mode="json")
    match canonicalize(value):
        case Ok(value=text):
            return text.encode("utf-8")
        case Err(error=reason):
            raise ValueError(reason)


PERMISSIONS = 0o777
"""The mode bits an import keeps: setuid, setgid and sticky (0o7000) are masked off."""


def masked(tree: Tree) -> Tree:
    """The tree as an import of its built archive leaves it: every file and dir mode masked."""
    return Tree(
        tree_version=tree.tree_version,
        entries=[
            e if isinstance(e, TreeSymlink) else e.model_copy(update={"mode": e.mode & PERMISSIONS})
            for e in tree.entries
        ],
    )


def tree_manifest_hash(tree: Tree) -> str:
    """The snapshot manifest hash of the tree's files (spec/schema/README.md, Snapshot
    manifest)."""
    return manifest_hash(
        [
            ManifestEntry(path=e.path, mode=e.mode, size=e.size, sha256=e.sha256)
            for e in tree.entries
            if isinstance(e, TreeFile)
        ]
    )


def broken(entries: Sequence[TreeEntry]) -> str | None:
    """The first semantic rule (2 to 5) the entries break, in order; None when they keep every
    rule. The schema can't hold these, so every tree is checked before it is trusted or built."""
    paths = PathSet()
    last: bytes | None = None
    for e in entries:
        key = utf16_key(e.path)
        if last is not None and not last < key:
            return f"{e.path} is out of order"
        last = key
        if not is_normal(e.path):
            return f"{e.path} is not a normal path"
        if isinstance(e, TreeSymlink) and not in_root(e.path, e.target):
            return f"{e.path} links outside the root"
        clash = paths.add(e.path, e.kind)
        if clash is not None:
            return f"{e.path}: {clash}"
    return None


def parse_tree(data: bytes) -> Ok[Tree] | Err[ParseError]:
    """Parses a tree artifact's bytes (storage is a boundary): strict JSON in canonical form,
    the schema, then the semantic rules. Anything else is artifact_corrupt."""

    def corrupt(why: str) -> Err[ParseError]:
        return Err(ParseError("artifact_corrupt", f"tree {sha256_hex(data)} is invalid: {why}"))

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return corrupt("not UTF-8")
    match parse_json(text):
        case Err(error=reason):
            return corrupt(reason)
        case Ok(value=value):
            pass
    try:
        tree = Tree.model_validate(value)
    except ValidationError as error:
        return corrupt(str(error))
    if encode_tree(tree) != data:
        return corrupt("not in canonical form")
    why = broken(tree.entries)
    return Ok(tree) if why is None else corrupt(why)

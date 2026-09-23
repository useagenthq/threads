"""JSON read from spec/ files, narrowed to the shape the caller expects. The narrowing helpers
are the renderer's (spec/tools/typeexpr_render.py), so docs and checks share one Json type."""

import json
from pathlib import Path

from typeexpr_render import Json, Obj, array, obj, objs, text

__all__ = ["Json", "Obj", "array", "doc_of", "load", "obj", "objs", "text"]


def load(path: Path) -> Json:
    return json.loads(path.read_text())


def doc_of(node: Obj) -> str:
    """The node's doc prose, or "" when it has none."""
    return text(node.get("doc", ""))

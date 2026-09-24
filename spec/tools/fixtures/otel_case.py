# pyright: strict
"""Writing an OpenTelemetry golden: spec/otel/cases/<name>/.

    case.json         what to export: the branches, the options, and each sync in order
    <branch_id>.jsonl each branch's export (a fork's holds its parent's prefix)
    sync-<n>.otlp.json the exact request body sync n sends (no file when it sends nothing)

A sync reads its branch's chain through `head_seq` and exports the spans that close in
(`cursor_before`, `head_seq`]; the cursor is then `cursor_after`. Other branches of the case are
what `lookup` reads (a child's parent).
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .common import obj, text
from .otel_expect import RESOURCE, SCOPE_VERSION, Span, body
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .log import Log

README = pathlib.Path(__file__).resolve().parents[2] / "otel" / "README.md"
TENANT = "local"


@dataclass(frozen=True)
class Sync:
    branch: str
    head_seq: int
    cursor_before: int
    spans: list[Span]


def pinned_names() -> frozenset[str]:
    """The gen_ai.* names spec/otel/README.md pins (its `names` block)."""
    found = re.search(r"```names\n(.*?)```", README.read_text(), flags=re.DOTALL)
    if found is None:
        raise AssertionError("spec/otel/README.md has no names block")
    return frozenset(found.group(1).split())


def _check_names(spans: list[Span]) -> None:
    allowed = pinned_names()
    for s in spans:
        attrs = s.json["attributes"]
        for a in attrs if isinstance(attrs, list) else []:
            key = text(obj(a)["key"])
            if key.startswith("gen_ai.") and key not in allowed:
                raise AssertionError(f"{key} is not a pinned semconv v1.41.1 name")


def write(  # noqa: PLR0913, PLR0917 - one golden's parts
    root: pathlib.Path,
    name: str,
    description: str,
    logs: list[Log],
    syncs: list[Sync],
    content: bool = False,
) -> None:
    d = root / name
    d.mkdir(parents=True)
    for log in logs:
        (d / f"{log.branch}.jsonl").write_bytes(log.export())
        # The request bytes each model_request names, so a store can import the log.
        for digest, data in sorted(log.artifacts.items()):
            (d / "artifacts").mkdir(exist_ok=True)
            (d / "artifacts" / digest).write_bytes(data)
    planned: list[JsonValue] = []
    for n, s in enumerate(syncs, start=1):
        entry: Obj = {
            "branch_id": s.branch,
            "head_seq": s.head_seq,
            "cursor_before": s.cursor_before,
            "cursor_after": s.head_seq,
            "spans": len(s.spans),
        }
        if s.spans:
            _check_names(s.spans)
            if any(not s.cursor_before < sp.close_seq <= s.head_seq for sp in s.spans):
                raise AssertionError(f"{name}: sync {n} expects a span outside its window")
            entry["expected"] = f"sync-{n}.otlp.json"
            (d / f"sync-{n}.otlp.json").write_bytes(body(s.spans))
        planned.append(entry)
    case: Obj = {
        "name": name,
        "description": description,
        "tenant": TENANT,
        "content": content,
        "resource": RESOURCE,
        "scope_version": SCOPE_VERSION,
        "branches": [log.branch for log in logs],
        "syncs": planned,
    }
    (d / "case.json").write_text(dump(case))

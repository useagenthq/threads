"""Write every generated file, or with --check report the ones that are out of date."""

import json
import sys
from pathlib import Path

from api_docs import check_docs

from .json_access import Json, Obj, load, obj, text
from .members import Explain
from .openapi import bundle_openapi
from .overview import overview_page, sidebar_meta, type_groups
from .pages import function_page, type_page
from .tables import OPENAPI, REF, ROOT, SPEC


def schemas() -> dict[str, Json]:
    """The wire schemas api.json references, by $id (fields may inherit their descriptions)."""
    paths = [*(SPEC / "schema").rglob("*.json"), SPEC / "conformance" / "case.schema.json"]
    docs = [load(p) for p in paths]
    return {text(d["$id"]): d for d in docs if isinstance(d, dict) and "$id" in d}


def outputs(api: Obj, explain: Explain) -> dict[Path, str]:
    files: dict[Path, str] = {}
    for key, value in obj(api["functions"]).items():
        f = obj(value)
        files[REF / "functions" / f"{text(f['ts'])}.mdx"] = function_page(explain, api, key, f)
    groups = type_groups(api)
    for name, t in obj(api["types"]).items():
        files[REF / "types" / f"{name}.mdx"] = type_page(explain, name, obj(t))
    decisions = obj(load(SPEC / "api-surface-factory-decisions.json"))
    files[REF / "overview.mdx"] = overview_page(api, groups, decisions)
    files[OPENAPI] = json.dumps(bundle_openapi(), indent=2) + "\n"
    files[REF / "meta.json"] = sidebar_meta(api, groups)
    return files


def stale_pages(files: dict[Path, str]) -> list[Path]:
    """Generated pages whose member is gone from api.json."""
    return [
        p
        for d in (REF / "functions", REF / "types")
        if d.exists()
        for p in d.glob("*.mdx")
        if p not in files
    ]


def main() -> int:
    check = "--check" in sys.argv[1:]
    api, wire = obj(load(SPEC / "api.json")), schemas()
    unexplained = check_docs(api, wire)
    if unexplained:
        # The same rule as check_api.py: every row the pages print needs an explanation.
        print("\n".join(unexplained), file=sys.stderr)
        return 1
    files = outputs(api, Explain(obj(api["types"]), wire))
    stale = stale_pages(files)
    changed = [p for p, content in files.items() if not p.exists() or p.read_text() != content]
    if check:
        for p in changed + stale:
            print(f"out of date: {p.relative_to(ROOT)}")
        return 1 if changed or stale else 0
    for p in stale:
        p.unlink()
    for p in changed:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(files[p])
    print(f"wrote {len(changed)} file(s), removed {len(stale)}")
    return 0

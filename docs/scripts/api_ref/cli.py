"""Write every generated file, or with --check report the ones that are out of date."""

import json
import sys
from pathlib import Path

from .json_access import load, obj, text
from .openapi import bundle_openapi
from .overview import overview_page, sidebar_meta, type_groups
from .pages import function_page, type_page
from .tables import OPENAPI, REF, ROOT, SPEC


def outputs() -> dict[Path, str]:
    api = obj(load(SPEC / "api.json"))
    files: dict[Path, str] = {}
    for key, value in obj(api["functions"]).items():
        f = obj(value)
        files[REF / "functions" / f"{text(f['ts'])}.mdx"] = function_page(api, key, f)
    groups = type_groups(api)
    for name, t in obj(api["types"]).items():
        files[REF / "types" / f"{name}.mdx"] = type_page(name, obj(t))
    files[REF / "overview.mdx"] = overview_page(api, groups)
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
    files = outputs()
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

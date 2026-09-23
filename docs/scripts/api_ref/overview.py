"""The reference overview page and the sidebar that lists every page."""

import json
from pathlib import Path
from string import Template

from .json_access import Obj, doc_of, obj, text
from .tables import GROUP_ICONS, TYPE_GROUPS
from .text import first_sentence, frontmatter, mdx

TEMPLATE = Template((Path(__file__).parent / "overview.md").read_text())

type Groups = list[tuple[str, list[str]]]


def type_groups(api: Obj) -> Groups:
    """TYPE_GROUPS restricted to the types in api.json, plus any type no group names."""
    types = obj(api["types"])
    grouped = {n for _, names in TYPE_GROUPS for n in names}
    groups = [(g, [n for n in names if n in types]) for g, names in TYPE_GROUPS]
    extra = [n for n in types if n not in grouped]
    if extra:
        groups.append(("Other types", extra))
    return groups


def overview_page(api: Obj, groups: Groups) -> str:
    rows = "\n".join(
        f"| {k} | `{text(p['ts'])}` | `{text(p['py'])}` |"
        for k, p in ((k, obj(v)) for k, v in obj(api["packages"]).items())
    )
    fns = "\n".join(
        f"| [`{text(f['ts'])}`](/docs/reference/functions/{text(f['ts'])}) | `{text(f['py'])}` | "
        f"{mdx(first_sentence(doc_of(f)))} |"
        for f in (obj(v) for v in obj(api["functions"]).values())
    )
    links = "\n\n".join(
        f"**{g}:** " + ", ".join(f"[`{n}`](/docs/reference/types/{n})" for n in names)
        for g, names in groups
    )
    title = frontmatter(
        "API reference", "Every public function and type, in TypeScript and Python.", "BookOpen"
    )
    return title + "\n" + TEMPLATE.substitute(rows=rows, fns=fns, groups=links)


def sidebar_meta(api: Obj, groups: Groups) -> str:
    """The API Reference sidebar tab: a Fumadocs root folder, grouped by separators."""
    pages = [
        "overview",
        "---[SquareFunction]Functions---",
        *(f"functions/{text(obj(f)['ts'])}" for f in obj(api["functions"]).values()),
    ]
    for g, names in groups:
        pages += [f"---[{GROUP_ICONS.get(g, 'Braces')}]{g}---", *(f"types/{n}" for n in names)]
    meta = {
        "title": "API Reference",
        "description": "Functions and types",
        "icon": "SquareCode",
        "root": True,
        "pages": pages,
    }
    return json.dumps(meta, indent=2) + "\n"

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


def providers(api: Obj) -> list[Obj]:
    """Functions of adapter packages: the provider factories."""
    packages = obj(api["packages"])
    return [
        f
        for f in (obj(v) for v in obj(api["functions"]).values())
        if obj(packages[text(f.get("package", "core"))]).get("kind") == "adapter"
    ]


def _cell(p: Obj, lang: str) -> str:
    return f"`{text(p[lang])}`" if lang in p else "—"


def _function_rows(fns: list[Obj]) -> str:
    return "\n".join(
        f"| [`{text(f['ts'])}`](/docs/reference/functions/{text(f['ts'])}) | `{text(f['py'])}` | "
        f"{mdx(first_sentence(doc_of(f)))} |"
        for f in fns
    )


def pending_list(decisions: Obj) -> str:
    """Providers that exist but whose factory is not in the contract yet."""
    return "\n".join(
        f"- `{name}`: {mdx(text(obj(f)['pending']))}"
        for name, f in obj(decisions["factories"]).items()
        if "pending" in obj(f)
    )


def overview_page(api: Obj, groups: Groups, decisions: Obj) -> str:
    rows = "\n".join(
        f"| {k} | {_cell(p, 'ts')} | {_cell(p, 'py')} |"
        for k, p in ((k, obj(v)) for k, v in obj(api["packages"]).items())
    )
    listed = providers(api)
    core = [f for f in (obj(v) for v in obj(api["functions"]).values()) if f not in listed]
    links = "\n\n".join(
        f"**{g}:** " + ", ".join(f"[`{n}`](/docs/reference/types/{n})" for n in names)
        for g, names in groups
    )
    title = frontmatter(
        "API reference", "Every public function and type, in TypeScript and Python.", "BookOpen"
    )
    return (
        title
        + "\n"
        + TEMPLATE.substitute(
            rows=rows,
            fns=_function_rows(core),
            providers=_function_rows(listed),
            pending=pending_list(decisions),
            groups=links,
        )
    )


def sidebar_meta(api: Obj, groups: Groups) -> str:
    """The API Reference sidebar tab: a Fumadocs root folder, grouped by separators."""
    listed = providers(api)
    fns = [obj(f) for f in obj(api["functions"]).values()]
    pages = [
        "overview",
        "---[SquareFunction]Functions---",
        *(f"functions/{text(f['ts'])}" for f in fns if f not in listed),
        "---[Plug]Providers---",
        *(f"functions/{text(f['ts'])}" for f in listed),
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

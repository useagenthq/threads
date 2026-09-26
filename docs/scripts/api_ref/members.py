"""The Field lists of parameters, fields and properties, with their docs."""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from api_docs import Entry, Kind, resolved_doc, walk_type
from api_factories import expanded_errors, langs
from typeexpr_render import Render, camel, ref_name

from .json_access import Json, Obj, array, obj, text
from .tables import CHANGED, DOC_OVERRIDES, LANG_LABEL, NOT_BUILT, NOTES, ONLY_IN
from .text import attr, clean, field


def annotate(container: str, members: list[Obj]) -> list[Obj]:
    """Drop members that are not built and mark one-language members."""
    out: list[Obj] = []
    for member in members:
        name = text(member["name"])
        if (container, name) in NOT_BUILT:
            continue
        m = dict(member)
        lang = ONLY_IN.get((container, name))
        if lang:
            m["lang"] = lang
        if (container, name) in DOC_OVERRIDES:
            m["doc"] = DOC_OVERRIDES[(container, name)]
        out.append(m)
    return out


def member_doc(container: str, name: str | None, doc: str | None, lang: str | None) -> str:
    parts: list[str] = []
    if (container, name) in CHANGED:
        parts.append(CHANGED[(container, name)])
    if lang:
        parts.append(f"{LANG_LABEL[lang]} only.")
    if doc:
        parts.append(clean(doc))
    if (container, name) in NOTES:
        parts.append(NOTES[(container, name)])
    return " ".join(p for p in parts if p)


def lang_of(member: Obj) -> str | None:
    lang = member.get("lang")
    return None if lang is None else text(lang)


def labels(name: str, casing: str = "api") -> tuple[str, str]:
    """A member's TypeScript and Python names."""
    return (camel(name) if casing == "api" else name, name)


def label_text(names: tuple[str, str], lang: str | None) -> str:
    ts, py = names
    if lang == "ts":
        return ts
    if lang == "py":
        return py
    return ts if ts == py else f"{ts} / {py}"


@dataclass(frozen=True, slots=True)
class Explain:
    """Each row's explanation, by the one rule check_api.py enforces (spec/tools/api_docs.py)."""

    types: Obj
    schemas: Mapping[str, Json]

    def __call__(self, entry: Entry) -> str:
        doc = resolved_doc(entry, self.types, self.schemas)
        if doc is None:
            raise ValueError(f"api.json {entry.path} ({entry.kind}) has no doc: see check_api.py")
        return doc


def row(
    names: tuple[str, str], node: Mapping[str, Json], type_text: str, lang: str | None, doc: str
) -> str:
    attrs = [f'name="{label_text(names, lang)}"', f"type={attr(type_text)}"]
    if node.get("required", True):
        attrs.append("required")
    if "default" in node:
        value = node["default"]
        attrs.append(f"default={attr(value if isinstance(value, str) else json.dumps(value))}")
    return field(attrs, doc)


def nested_rows(
    explain: Explain, parent_row: Entry, names: tuple[str, str], lang: str | None
) -> list[str]:
    """One row per inline object field, callback parameter and callback return field inside the
    parent row's type, at any depth, labelled from the parent: web.fetch, execute(ctx),
    beforeTool()[deny].reason."""
    known = {parent_row.path: names}
    rows: list[str] = []
    for entry in walk_type(parent_row.node["type"], parent_row.path, parent_row.casing):
        parent, _, name = entry.path.rpartition(".")
        ts, py = label_of(known, parent)
        own_ts, own_py = labels(name, "api" if entry.kind == "callback_param" else entry.casing)
        if entry.kind == "callback_param":
            known[entry.path] = (f"{ts}({own_ts})", f"{py}({own_py})")
        else:
            known[entry.path] = (f"{ts}.{own_ts}", f"{py}.{own_py}")
        type_text = Render(lang or "ts").expr(obj(entry.node["type"]), entry.casing)
        doc = member_doc(parent, name, explain(entry), lang)
        rows.append(row(known[entry.path], entry.node, type_text, lang, doc))
    return rows


def label_of(known: dict[str, tuple[str, str]], path: str) -> tuple[str, str]:
    """A row's label; a path that has no row of its own (the "()" of a callback's return, a
    union alternative's "[tag]") extends its nearest labelled ancestor."""
    base = path
    while base not in known:
        cut = max(base.rfind("("), base.rfind("["))
        if cut < 0:
            raise KeyError(f"{path} has no labelled ancestor")
        base = base[:cut]
    ts, py = known[base]
    suffix = path[len(base) :]
    return ts + suffix, py + suffix


def param_field(explain: Explain, container: str, p: Obj) -> str:
    name = text(p["name"])
    lang = lang_of(p)
    ptype = obj(p["type"])
    path = f"{container}.{name}"
    kind: Kind = "option" if p["kind"] == "option" else "param"
    entry = Entry(p, kind, path)
    doc = member_doc(container, name, explain(entry), lang)
    ref = ptype.get("$ref")
    if isinstance(ref, str):
        type_name, link = ref_name(ref)
        if link:
            doc += f" See [{type_name or 'type'}]({link})."
    type_text = Render(lang or "ts").expr(ptype)
    rows = [row(labels(name), p, type_text, lang, doc)]
    return "\n\n".join(rows + nested_rows(explain, entry, labels(name), lang))


def param_fields(explain: Explain, container: str, params: list[Obj]) -> str:
    return "\n\n".join(param_field(explain, container, p) for p in params)


def fields_section(explain: Explain, container: str, fields: Obj, casing: str, kind: Kind) -> str:
    items: list[str] = []
    for name, value in fields.items():
        if (container, name) in NOT_BUILT:
            continue
        spec = obj(value)
        lang = ONLY_IN.get((container, name), lang_of(spec))
        path = f"{container}.{name}"
        names = labels(name, casing)
        ftype = obj(spec["type"])
        type_text = Render(lang or "ts").expr(ftype, casing)
        entry = Entry(spec, kind, path, casing)
        doc = member_doc(container, name, explain(entry), lang)
        items.append(row(names, spec, type_text, lang, doc))
        items += nested_rows(explain, entry, names, lang)
    return "\n\n".join(items)


_THROWN = {
    "ConfigError": " for a definition that can't run",
    "StoreCorruptError": " when the stored log doesn't verify",
}


def errors_line(spec: Obj) -> str:
    errors = obj(spec.get("returns") or {}).get("errors")
    lines: list[str] = []
    if errors:
        codes = ", ".join(f"`{e}`" for e in array(errors))
        lines.append(f"**Returns an error value** with one of these codes: {codes}.")
    throws = spec.get("throws")
    if throws:
        thrown = [str(t) for t in array(throws)]
        names = ", ".join(f"`{t}`" for t in thrown)
        why = _THROWN.get(thrown[0], "") if len(thrown) == 1 else ""
        lines.append(f"**Throws** {names}{why}.")
    return "\n\n".join(lines)


def refusals_line(f: Obj) -> str:
    """A factory's config_errors, with the language named on a code that applies to only one of
    the languages the factory exists in."""
    by_code: dict[str, set[str]] = {}
    for code, lang in expanded_errors(f):
        by_code.setdefault(code, set()).add(lang)
    items = [
        f"`{code}`" if found == set(langs(f)) else f"`{code}` ({LANG_LABEL[min(found)]} only)"
        for code, found in by_code.items()
    ]
    if not items:
        return ""
    return f"**Throws** `ConfigError` with one of these codes: {', '.join(items)}."

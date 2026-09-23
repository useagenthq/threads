"""The Field lists of parameters, fields and properties, with their docs."""

import json

from .json_access import Obj, array, doc_of, obj, text
from .render import Render, ref_name
from .tables import DOC_OVERRIDES, LANG_LABEL, NOT_BUILT, NOTES, ONLY_IN
from .text import attr, camel, clean, field


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


def param_label(name: str, lang: str | None) -> str:
    ts_name = camel(name)
    if lang == "ts":
        return ts_name
    if lang == "py":
        return name
    return name if ts_name == name else f"{ts_name} / {name}"


def param_field(container: str, p: Obj) -> str:
    name = text(p["name"])
    lang = lang_of(p)
    ptype = obj(p["type"])
    attrs = [f'name="{param_label(name, lang)}"', f"type={attr(Render(lang or 'ts').expr(ptype))}"]
    if p.get("required"):
        attrs.append("required")
    value = p.get("default")
    if "default" in p and value not in ([], {}, None):
        attrs.append(f"default={attr(value if isinstance(value, str) else json.dumps(value))}")
    doc = member_doc(container, name, doc_of(p), lang)
    ref = ptype.get("$ref")
    if isinstance(ref, str):
        type_name, link = ref_name(ref)
        if link:
            doc = (doc + " " if doc else "") + f"See [{type_name or 'type'}]({link})."
    return field(attrs, doc)


def param_fields(container: str, params: list[Obj]) -> str:
    return "\n\n".join(param_field(container, p) for p in params)


def fields_section(container: str, fields: Obj, casing: str) -> str:
    items: list[str] = []
    for name, value in fields.items():
        if (container, name) in NOT_BUILT:
            continue
        spec = obj(value)
        lang = ONLY_IN.get((container, name), lang_of(spec))
        ts_name = camel(name) if casing == "api" else name
        label = name if ts_name == name else f"{ts_name} / {name}"
        render = Render(lang or "ts")
        attrs = [f'name="{label}"', f"type={attr(render.expr(obj(spec['type']), casing))}"]
        if spec.get("required", True):
            attrs.append("required")
        items.append(field(attrs, member_doc(container, name, doc_of(spec), lang)))
    return "\n\n".join(items)


def errors_line(spec: Obj) -> str:
    errors = obj(spec.get("returns") or {}).get("errors")
    lines: list[str] = []
    if errors:
        codes = ", ".join(f"`{e}`" for e in array(errors))
        lines.append(f"**Returns an error value** with one of these codes: {codes}.")
    throws = spec.get("throws")
    if throws:
        names = ", ".join(f"`{t}`" for t in array(throws))
        lines.append(f"**Throws** {names} for a definition that can't run.")
    return "\n\n".join(lines)

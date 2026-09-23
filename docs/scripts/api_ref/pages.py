"""The function and type reference pages."""

from typeexpr_render import Render, type_link

from .json_access import Obj, doc_of, obj, objs, text
from .members import (
    Explain,
    annotate,
    errors_line,
    fields_section,
    lang_of,
    member_doc,
    param_fields,
    refusals_line,
)
from .signatures import signature
from .tables import LANG_LABEL, NOT_BUILT, ONLY_IN
from .text import after_first_sentence, code_group, first_sentence, frontmatter, mdx

ROLE_ICONS = {"protocol": "Plug", "handle": "Box", "opaque": "Package"}

ROLE_LABELS = {
    "handle": "A handle returned by the library.",
    "protocol": "A protocol: adapters implement it.",
    "opaque": "Opaque: build it with its factory and pass it on; it has no public members.",
}


def both_names(node: Obj) -> str:
    """The title of a member: one name, or the TypeScript and Python names when they differ."""
    ts, py = text(node["ts"]), text(node["py"])
    return ts if ts == py else f"{ts} / {py}"


def import_line(pkg: Obj, lang: str | None) -> str:
    """Where to import a function from, in the languages it exists in."""
    where = [
        f"`{text(pkg[k])}` ({LANG_LABEL[k]})"
        for k in ("ts", "py")
        if k in pkg and lang in (None, k)
    ]
    return f"Import from {' or '.join(where)}."


def function_page(explain: Explain, api: Obj, key: str, f: Obj) -> str:
    params = annotate(key, objs(f["params"]))
    pkg = obj(obj(api["packages"])[text(f.get("package", "core"))])
    lang = lang_of(f)
    ts_sig = None if lang == "py" else signature(text(f["ts"]), f, params, "ts", method=False)
    py_sig = None if lang == "ts" else signature(text(f["py"]), f, params, "py", method=False)
    desc = first_sentence(doc_of(f)) or f"The {text(f['ts'])} function."
    icon = "Plug" if pkg.get("kind") == "adapter" else "SquareFunction"
    body = [
        frontmatter(both_names(f), desc, icon),
        mdx(after_first_sentence(doc_of(f))),
        "",
        import_line(pkg, lang),
        "",
        code_group(ts_sig, py_sig),
    ]
    if params:
        body += ["", "## Parameters", "", param_fields(explain, key, params)]
    ret = f.get("returns")
    if ret:
        ret = obj(ret)
        link = type_link(ret)
        name = Render("ts").expr(obj(ret.get("result", ret)))
        body += ["", "## Returns", "", f"[`{name}`]({link})" if link else f"`{name}`"]
    for extra in (errors_line(f), refusals_line(f)):
        if extra:
            body += ["", extra]
    return "\n".join(body) + "\n"


def method_section(explain: Explain, type_name: str, name: str, m: Obj) -> str:
    container = f"{type_name}.{name}"
    params = annotate(container, objs(m.get("params", [])))
    lang = ONLY_IN.get((type_name, name), lang_of(m))
    ts_sig = None if lang == "py" else signature(text(m["ts"]), m, params, "ts", method=True)
    py_sig = None if lang == "ts" else signature(text(m["py"]), m, params, "py", method=True)
    out = [f"### {both_names(m)}", ""]
    doc = member_doc(type_name, name, doc_of(m), lang)
    if doc:
        out += [mdx(doc), ""]
    out.append(code_group(ts_sig, py_sig))
    if params:
        out += ["", param_fields(explain, container, params)]
    extra = errors_line(m)
    if extra:
        out += ["", extra]
    return "\n".join(out)


def built(container: str, members: Obj) -> Obj:
    return {k: v for k, v in members.items() if (container, k) not in NOT_BUILT}


def shape_section(explain: Explain, name: str, t: Obj, casing: str) -> list[str]:
    """An alias's definition, an object's fields or a union's variants."""
    kind = t["kind"]
    if kind == "alias":
        ts, py = Render("ts").expr(obj(t["type"])), Render("py").expr(obj(t["type"]))
        return [code_group(f"type {name} = {ts};", f"type {name} = {py}")]
    if kind == "object":
        return ["## Fields", "", fields_section(explain, name, obj(t["fields"]), casing, "field")]
    if kind != "union":
        return []
    disc = text(t["discriminator"])
    out = [f"A union discriminated by `{disc}`.", ""]
    for variant in objs(t["variants"]):
        fields = obj(variant["object"])
        tag = obj(obj(fields.get(disc, {})).get("type", {})).get("literal", "variant")
        variant_casing = text(variant.get("casing", casing))
        out += [
            f"### {tag}",
            "",
            fields_section(explain, name, fields, variant_casing, "field"),
            "",
        ]
    return out


def type_page(explain: Explain, name: str, t: Obj) -> str:
    role = text(t.get("role", ""))
    desc = first_sentence(doc_of(t)) or f"The {name} type."
    body = [frontmatter(name, desc, ROLE_ICONS.get(role, "Braces"))]
    doc = member_doc(name, None, after_first_sentence(doc_of(t)), None)
    if doc:
        body += [mdx(doc), ""]
    if role in ROLE_LABELS:
        body += [f"<Callout>{ROLE_LABELS[role]}</Callout>", ""]
    casing = text(t.get("casing", "api"))
    body += shape_section(explain, name, t, casing)
    props = built(name, obj(t.get("properties", {})))
    if props:
        body += ["## Properties", "", fields_section(explain, name, props, casing, "property"), ""]
    methods = built(name, obj(t.get("methods", {})))
    if methods:
        body += ["## Methods", ""]
        for key, m in methods.items():
            body += [method_section(explain, name, key, obj(m)), ""]
    return "\n".join(body).rstrip() + "\n"

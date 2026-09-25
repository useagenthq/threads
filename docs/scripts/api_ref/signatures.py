"""Function and method signatures, as each language declares them."""

from typeexpr_render import Render, default_text, visible

from .json_access import Obj, obj, text
from .tables import PY_TYPES


def signature(name: str, spec: Obj, members: list[Obj], lang: str, method: bool) -> str:
    """The declaration of spec with its built params (members), in one language."""
    params = [p for p in members if visible(p, lang)]
    is_async = bool(spec.get("async"))
    ret = Render(lang).returns(spec, is_async)
    positional = [p for p in params if p["kind"] == "positional"]
    options = [p for p in params if p["kind"] == "option"]
    if lang == "ts":
        head = f"{name}(" if method else f"function {name}("
        return f"{head}{ts_params(positional, options)}): {ret}"
    lines = py_params(name, positional, options)
    keyword = "async def" if is_async else "def"
    if not lines:
        return f"{keyword} {name}() -> {ret}"
    return f"{keyword} {name}(\n" + "\n".join(lines) + f"\n) -> {ret}"


def ts_params(positional: list[Obj], options: list[Obj]) -> str:
    """Positional parameters, then one trailing options object."""
    r = Render("ts")
    lines = [
        f"{r.key(text(p['name']), 'api')}{'' if p.get('required') else '?'}: "
        f"{r.expr(obj(p['type']))}"
        for p in positional
    ]
    if options:
        body = "\n".join(
            f"  {r.key(text(p['name']), 'api')}{'' if p.get('required') else '?'}: "
            f"{r.expr(obj(p['type']))};"
            for p in options
        )
        optional = "" if any(p.get("required") for p in options) else "?"
        lines.append(f"options{optional}: {{\n{body}\n}}")
    return ", ".join(lines)


def py_params(name: str, positional: list[Obj], options: list[Obj]) -> list[str]:
    """One line per parameter: positional first, then keyword-only options."""
    r = Render("py")
    # A positional's declared default isn't spelled (the Python default may be a None stand-in).
    lines = [
        _py_param(
            text(p["name"]),
            r.expr(obj(p["type"])),
            {k: v for k, v in p.items() if k != "default"}
            | {"required": p.get("required") or "default" in p},
        )
        for p in positional
    ]
    if options:
        lines.append("    *,")
    for p in options:
        pname = text(p["name"])
        native = PY_TYPES.get((name, pname))
        ptype = r.expr({"native": {"py": native}} if native else obj(p["type"]))
        lines.append(_py_param(pname, ptype, p))
    return lines


def _py_param(pname: str, ptype: str, p: Obj) -> str:
    """Required: no default; a declared literal default; else `T | None = None`."""
    if p.get("required"):
        return f"    {pname}: {ptype},"
    if "default" in p:
        return f"    {pname}: {ptype} = {default_text(p['default'], 'py')},"
    return f"    {pname}: {ptype} | None = None,"

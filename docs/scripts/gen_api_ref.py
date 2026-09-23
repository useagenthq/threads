"""Generate the API reference pages from spec/api.json and the bundled HTTP OpenAPI file.

Stdlib only and deterministic: the same spec always writes the same bytes.

    python3 docs/scripts/gen_api_ref.py          # write content/docs/reference/** and openapi.json
    python3 docs/scripts/gen_api_ref.py --check  # exit 1 if anything is out of date

The pages are Fumadocs MDX. The HTTP pages are generated from docs/openapi.json by
docs/scripts/gen-openapi.ts.

spec/api.json is the contract, and it also lists members that are not built yet. Those are
kept out of the docs by NOT_BUILT, and members built in one language only are marked by
ONLY_IN. Update both tables when a member lands.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "spec"
DOCS = ROOT / "docs"
REF = DOCS / "content" / "docs" / "reference"
OPENAPI = DOCS / "openapi.json"

# (container, member): in spec/api.json, not in either implementation yet.
NOT_BUILT = {
    ("agent", "output_mode"),
    ("agent", "browser"),
    ("agent", "output_styles"),
    ("agent", "stream_release"),
    ("tool", "module"),
    ("tool", "entrypoint"),
    ("tool", "concurrent"),
    ("Thread", "replay"),
    ("Thread", "compact"),
    ("Thread", "setOutputStyle"),
    ("Thread", "usage"),
    ("Thread", "cost"),
    ("Thread", "cacheBreaks"),
    # Exists, but no tool asks the user a question yet (ask_user is not built).
    ("Thread", "answer"),
}

# HTTP routes left out for the same reason, by operationId.
NOT_BUILT_ROUTES = {"answerQuestion"}

# Titles of the generated HTTP pages, by operationId.
SUMMARIES = {
    "startRun": "Start a run",
    "subscribeRun": "Stream a run's events",
    "getTimeline": "Get a thread's timeline",
    "listBranches": "List branches",
    "listForkPoints": "List fork points",
    "fork": "Fork a thread",
    "listApprovals": "List pending approvals",
    "decideApproval": "Approve or deny",
    "resolveParked": "Resolve a parked action",
    "cancel": "Cancel a thread",
    "setModel": "Change the model",
    "setMode": "Change the permission mode",
    "channelChallenge": "Channel verification challenge",
    "channelWebhook": "Channel webhook",
}

# The address `threads dev` serves on, so the playground can build example requests.
DEV_SERVER = "http://localhost:8787"

# (container, member): built in one language only.
ONLY_IN = {
    ("agent", "output"): "ts",
    ("agent", "output_retries"): "ts",
    ("agent", "fallback"): "ts",
    ("agent", "on_unknown_usage"): "py",
    ("tool", "output"): "ts",
    ("Agent", "check"): "ts",
}

# Extra sentences where today's behavior is narrower than the contract.
NOTES = {
    ("tool", "runs"): 'Only "host" is supported today; "sandbox" is a setup error.',
    ("mcp", "runs"): 'Only "host" is supported today; "sandbox" is a setup error.',
    ("agent", "egress"): 'Host allowlists are not supported yet in either language: use [] '
    '(deny-all) or "unenforced".',
    ("Agent", "run"): "Python: an agent with app tools needs deps on every run; "
    "pass deps=None when its tools take none.",
    ("Thread", "forkPoints"): "TypeScript returns the list itself; Python returns Ok or Err.",
    ("Thread", "todos"): "TypeScript returns the list itself; Python returns Ok or Err.",
    ("Thread", "children"): "TypeScript returns the list itself; Python returns Ok or Err.",
    ("RunResult", None): "In TypeScript, thread is a ThreadRef (id, branch, store): pass it to "
    "openThread for the full Thread handle. In Python it is the Thread handle.",
}

# Contract text that only makes sense next to the spec, rewritten for readers.
DOC_OVERRIDES = {
    ("Thread.fork", "mode"): "stub runs the new branch without live side effects: every "
    "mediated call is answered from what the original branch recorded after the fork point. "
    "Stubbing works in Python only today; in TypeScript a run on a stub fork is live. A "
    "stub-mode run on a live model that declares hosted tools is refused with ConfigError "
    "hosted_tool_unsupported, because hosted calls can't be stubbed.",
}

# Python spellings that differ from the contract's inline shape.
PY_TYPES = {("tool", "reconcile"): "Reconcile[Output, Deps]"}

LANG_LABEL = {"ts": "TypeScript", "py": "Python"}

TYPE_GROUPS = [
    (
        "Agents and runs",
        ["Agent", "RunResult", "RunStream", "RunContext", "Tool", "McpServer", "Extension",
         "Hooks", "Skill", "Secret", "Store", "ConfigError", "ConfigErrorCode"],
    ),
    ("Threads and evals", ["Thread", "CaseExpectation", "SavedCase"]),
    ("Host", ["Host", "Schedule"]),
    (
        "Model adapters",
        ["Model", "ModelContext", "ModelInfo", "ModelRequest", "ModelResponse", "ModelChunk",
         "LookupResult", "LookupCapability"],
    ),
    (
        "Sandbox adapters",
        ["Sandbox", "SandboxAuthority", "SandboxContext", "SandboxInfo", "SandboxSession",
         "ExecOutput", "ExecResult"],
    ),
    (
        "Memory and knowledge adapters",
        ["MemoryProvider", "KnowledgeProvider", "Scope", "Binding", "MemoryRecord", "RecordRef",
         "MemoryHit", "KnowledgeSource", "DocVersion", "KnowledgeHit", "Doc", "SearchBackend",
         "SearchHit"],
    ),
    (
        "Channel adapters",
        ["ChannelAdapter", "ChannelCapabilities", "RawRequest", "RawResponse",
         "VerifiedDelivery", "Inbound", "DeliveryOutcome"],
    ),
]

# Sidebar separator icons (lucide names, as Fumadocs resolves them).
GROUP_ICONS = {
    "Agents and runs": "Bot",
    "Threads and evals": "RotateCcwClock",
    "Host": "Server",
    "Model adapters": "Cpu",
    "Sandbox adapters": "Box",
    "Memory and knowledge adapters": "Brain",
    "Channel adapters": "MessageCircle",
}

# ---------------------------------------------------------------- text


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def clean(text: str) -> str:
    """Contract prose without spec cross-references and internal vocabulary."""
    text = re.sub(r"\s*\([^()]*(?:\bF\d|\bC\d|spec/|AGENTS|ADR|conformance)[^()]*\)", "", text)
    text = re.sub(r"\s*\(policy\.\w+\)", "", text)
    for old, new in (
        ("Render v1 line 0", "the pinned prompt prefix"),
        ("Line 0", "The system prompt"),
        ("line 0", "the system prompt"),
        ("Render v1 bytes", "The rendered request bytes"),
        ("a Render v1 line", "the rendered request"),
        ("Render v1", "the rendered request"),
    ):
        text = text.replace(old, new)
    return text.lstrip(". ").strip()


def mdx(text: str) -> str:
    """Escape prose for MDX: braces and angle brackets are JSX."""
    return text.replace("{", "&#123;").replace("}", "&#125;").replace("<", "&lt;")


def attr(text: str) -> str:
    return "{" + json.dumps(text) + "}"


# ---------------------------------------------------------------- types


def ref_name(ref: str) -> tuple[str, str | None]:
    """A readable name for a $ref, and the reference page it links to (types in this file)."""
    base, _, pointer = ref.partition("#")
    if not base:
        name = pointer.rsplit("/", 1)[-1]
        return name, f"/docs/reference/types/{name}"
    parts = [p for p in pointer.split("/") if p not in ("", "$defs", "properties")]
    if parts and parts[0].startswith("ev_"):
        parts[0] = "".join(w.capitalize() for w in parts[0][3:].split("_")) + "Event"
    return ".".join(parts), None


class Render:
    """One language's spelling of api.json type expressions."""

    def __init__(self, lang: str) -> None:
        self.lang = lang

    def key(self, name: str, casing: str) -> str:
        return camel(name) if self.lang == "ts" and casing == "api" else name

    def expr(self, t: dict, casing: str = "api") -> str:  # noqa: C901, PLR0911, PLR0912
        ts = self.lang == "ts"
        if "$ref" in t:
            name, _ = ref_name(t["$ref"])
            args = [self.expr(a, casing) for a in t.get("args", [])]
            if not args:
                return name
            return f"{name}<{', '.join(args)}>" if ts else f"{name}[{', '.join(args)}]"
        if "prim" in t:
            table = {
                "string": ("string", "str"),
                "integer": ("number", "int"),
                "number": ("number", "float"),
                "boolean": ("boolean", "bool"),
                "null": ("null", "None"),
                "bytes": ("Uint8Array", "bytes"),
                "unknown": ("unknown", "object"),
                "void": ("void", "None"),
            }
            return table[t["prim"]][0 if ts else 1]
        if "literal" in t:
            return json.dumps(t["literal"]) if ts else f"Literal[{json.dumps(t['literal'])}]"
        if "enum" in t:
            values = [json.dumps(v) for v in t["enum"]]
            return " | ".join(values) if ts else f"Literal[{', '.join(values)}]"
        if "generic" in t:
            return t["generic"]
        if "array" in t:
            inner = self.expr(t["array"], casing)
            if ts:
                return f"readonly ({inner})[]" if " " in inner else f"readonly {inner}[]"
            return f"Sequence[{inner}]"
        if "union" in t:
            return " | ".join(self.expr(v, casing) for v in t["union"])
        if "map" in t:
            inner = self.expr(t["map"], casing)
            return f"Record<string, {inner}>" if ts else f"Mapping[str, {inner}]"
        if "partial" in t:
            inner = self.expr(t["partial"], casing)
            return f"Partial<{inner}>" if ts else inner
        if "schema" in t:
            inner = self.expr(t["schema"], casing)
            return f"z.ZodType<{inner}>" if ts else f"type[{inner}]"
        if "promise" in t:
            inner = self.expr(t["promise"], casing)
            return f"Promise<{inner}>" if ts else f"Awaitable[{inner}]"
        if "stream" in t:
            inner = self.expr(t["stream"], casing)
            return f"AsyncIterable<{inner}>" if ts else f"AsyncIterator[{inner}]"
        if "native" in t:
            return t["native"].get(self.lang, "")
        if "object" in t:
            inner_casing = t.get("casing", casing)
            fields = [
                f"{self.key(k, inner_casing)}{'' if f.get('required', True) else '?'}: "
                f"{self.expr(f['type'], inner_casing)}"
                for k, f in t["object"].items()
            ]
            return "{ " + "; ".join(fields) + " }" if ts else "{" + ", ".join(fields) + "}"
        if "fn" in t:
            return self.fn(t["fn"], casing)
        if "result" in t:
            value = self.expr(t["result"], casing)
            return f"Result<{value}>" if ts else f"Ok[{value}] | Err[Failure]"
        if "type" in t:
            return self.expr(t["type"], casing)
        raise ValueError(f"unknown type expression {t}")

    def returns(self, spec: dict, is_async: bool) -> str:
        r = spec.get("returns")
        if r is None:
            text = "void" if self.lang == "ts" else "None"
        elif "stream" in r and "errors" in r:
            text = self.expr({"result": {"stream": r["stream"]}})
        else:
            text = self.expr(r)
        if is_async and self.lang == "ts":
            return f"Promise<{text}>"
        return text

    def fn(self, f: dict, casing: str) -> str:
        params = [p for p in f.get("params", []) if visible(p, self.lang)]
        ret = self.returns(f, bool(f.get("async")))
        if self.lang == "ts":
            args = ", ".join(f"{self.key(p['name'], casing)}: {self.expr(p['type'], casing)}"
                             for p in params)
            return f"({args}) => {ret}"
        args = ", ".join(self.expr(p["type"], casing) for p in params)
        return f"Callable[[{args}], {'Awaitable[' + ret + ']' if f.get('async') else ret}]"


def visible(member: dict, lang: str) -> bool:
    return member.get("lang", lang) == lang


def default_text(value: object, lang: str) -> str:
    if lang == "py":
        if value is True:
            return "True"
        if value is False:
            return "False"
        if value is None:
            return "None"
        if value == [] and lang == "py":
            return "()"
    return json.dumps(value)


# ---------------------------------------------------------------- annotate


def annotate(container: str, members: list[dict], key: str = "name") -> list[dict]:
    """Drop members that are not built and mark one-language members."""
    out = []
    for m in members:
        name = m[key]
        if (container, name) in NOT_BUILT:
            continue
        m = dict(m)
        lang = ONLY_IN.get((container, name))
        if lang:
            m["lang"] = lang
        if (container, name) in DOC_OVERRIDES:
            m["doc"] = DOC_OVERRIDES[(container, name)]
        out.append(m)
    return out


def member_doc(container: str, name: str | None, doc: str | None, lang: str | None) -> str:
    parts = []
    if lang:
        parts.append(f"{LANG_LABEL[lang]} only.")
    if doc:
        parts.append(clean(doc))
    if (container, name) in NOTES:
        parts.append(NOTES[(container, name)])
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- signatures


def signature(name: str, spec: dict, lang: str, method: bool) -> str:
    r = Render(lang)
    params = [p for p in spec.get("params", []) if visible(p, lang)]
    is_async = bool(spec.get("async"))
    ret = r.returns(spec, is_async)
    positional = [p for p in params if p["kind"] == "positional"]
    options = [p for p in params if p["kind"] == "option"]
    if lang == "ts":
        lines = [f"{r.key(p['name'], 'api')}: {r.expr(p['type'])}" for p in positional]
        if options:
            body = "\n".join(
                f"  {r.key(p['name'], 'api')}{'' if p.get('required') else '?'}: "
                f"{r.expr(p['type'])};"
                for p in options
            )
            optional = "" if any(p.get("required") for p in options) else "?"
            lines.append(f"options{optional}: {{\n{body}\n}}")
        head = f"{name}(" if method else f"function {name}("
        if len(lines) <= 1 and "\n" not in "".join(lines):
            return f"{head}{''.join(lines)}): {ret}"
        return f"{head}{', '.join(lines)}): {ret}"
    lines = [f"    {p['name']}: {r.expr(p['type'])}," for p in positional]
    if options:
        lines.append("    *,")
        for p in options:
            if (name, p["name"]) in PY_TYPES:
                p = {**p, "type": {"native": {"py": PY_TYPES[(name, p["name"])]}}}
            tail = ""
            if not p.get("required"):
                tail = f" = {default_text(p['default'], 'py')}" if "default" in p else " = None"
                if "default" not in p:
                    lines.append(f"    {p['name']}: {r.expr(p['type'])} | None{tail},")
                    continue
            lines.append(f"    {p['name']}: {r.expr(p['type'])}{tail},")
    keyword = "async def" if is_async else "def"
    if not lines:
        return f"{keyword} {name}() -> {ret}"
    return f"{keyword} {name}(\n" + "\n".join(lines) + f"\n) -> {ret}"


def code_group(ts: str | None, py: str | None) -> str:
    """Language tabs that stay in sync across the site (groupId "lang")."""
    blocks = [
        (LANG_LABEL[k], fence, code)
        for k, fence, code in (("ts", "ts", ts), ("py", "python", py))
        if code is not None
    ]
    if len(blocks) == 1:
        label, fence, code = blocks[0]
        return f'```{fence} title="{label}"\n{code}\n```'
    items = ", ".join(json.dumps(label) for label, _, _ in blocks)
    tabs = "\n".join(
        f'<Tab value="{label}">\n\n```{fence}\n{code}\n```\n\n</Tab>'
        for label, fence, code in blocks
    )
    return f'<Tabs items={{[{items}]}} groupId="lang" persist>\n{tabs}\n</Tabs>'


def type_link(t: dict) -> str | None:
    if "$ref" in t:
        return ref_name(t["$ref"])[1]
    for k in ("array", "promise", "stream", "map", "partial", "result"):
        if k in t and isinstance(t[k], dict):
            return type_link(t[k])
    return None


def param_fields(container: str, params: list[dict]) -> str:
    out = []
    for p in params:
        lang = p.get("lang")
        ts_name = camel(p["name"])
        label = p["name"] if ts_name == p["name"] else f"{ts_name} / {p['name']}"
        if lang == "ts":
            label = ts_name
        if lang == "py":
            label = p["name"]
        render = Render(lang or "ts")
        attrs = [f'name="{label}"', f"type={attr(render.expr(p['type']))}"]
        if p.get("required"):
            attrs.append("required")
        if "default" in p and p["default"] not in ([], {}, None):
            value = p["default"]
            attrs.append(f"default={attr(value if isinstance(value, str) else json.dumps(value))}")
        doc = member_doc(container, p["name"], p.get("doc"), lang)
        link = type_link(p["type"])
        if link:
            doc = (doc + " " if doc else "") + f"See [{ref_name(p['type'].get('$ref', '') or '')[0] or 'type'}]({link})." if "$ref" in p["type"] else doc
        out.append(field(attrs, doc))
    return "\n\n".join(out)


def errors_line(spec: dict) -> str:
    errors = (spec.get("returns") or {}).get("errors")
    lines = []
    if errors:
        lines.append(
            "**Returns an error value** with one of these codes: "
            + ", ".join(f"`{e}`" for e in errors)
            + "."
        )
    if spec.get("throws"):
        lines.append(
            "**Throws** "
            + ", ".join(f"`{t}`" for t in spec["throws"])
            + " for a definition that can't run."
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------- pages


def field(attrs: list[str], doc: str) -> str:
    if not doc:
        return f"<Field {' '.join(attrs)} />"
    return f"<Field {' '.join(attrs)}>\n  {mdx(doc)}\n</Field>"


def frontmatter(title: str, description: str, icon: str) -> str:
    return (
        f"---\ntitle: {json.dumps(title)}\ndescription: {json.dumps(description)}\n"
        f"icon: {json.dumps(icon)}\n---\n"
    )


def first_sentence(text: str) -> str:
    text = clean(text)
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return (match.group(1) if match else text) or ""


def after_first_sentence(text: str) -> str:
    """The prose after the sentence the page description already shows."""
    return clean(text)[len(first_sentence(text)) :].strip()


def function_page(api: dict, key: str, f: dict) -> str:
    params = annotate(key, f["params"])
    spec = {**f, "params": params}
    pkg = api["packages"][f["package"]]
    ts_sig = signature(f["ts"], spec, "ts", method=False)
    py_sig = signature(f["py"], spec, "py", method=False)
    desc = first_sentence(f.get("doc", "")) or f"The {f['ts']} function."
    body = [
        frontmatter(
            f["ts"] if f["ts"] == f["py"] else f"{f['ts']} / {f['py']}", desc, "SquareFunction"
        ),
        mdx(after_first_sentence(f.get("doc", ""))),
        "",
        f"Import from `{pkg['ts']}` (TypeScript) or `{pkg['py']}` (Python).",
        "",
        code_group(ts_sig, py_sig),
    ]
    if params:
        body += ["", "## Parameters", "", param_fields(key, params)]
    ret = f.get("returns")
    if ret:
        link = type_link(ret)
        name = Render("ts").expr(ret.get("result", ret))
        body += ["", "## Returns", "", f"[`{name}`]({link})" if link else f"`{name}`"]
    extra = errors_line(f)
    if extra:
        body += ["", extra]
    return "\n".join(body) + "\n"


def fields_section(container: str, fields: dict, casing: str) -> str:
    items = []
    for name, spec in fields.items():
        if (container, name) in NOT_BUILT:
            continue
        lang = ONLY_IN.get((container, name), spec.get("lang"))
        ts_name = camel(name) if casing == "api" else name
        label = name if ts_name == name else f"{ts_name} / {name}"
        render = Render(lang or "ts")
        attrs = [f'name="{label}"', f"type={attr(render.expr(spec['type'], casing))}"]
        if spec.get("required", True):
            attrs.append("required")
        doc = member_doc(container, name, spec.get("doc"), lang)
        items.append(field(attrs, doc))
    return "\n\n".join(items)


def method_section(type_name: str, name: str, m: dict) -> str:
    params = annotate(f"{type_name}.{name}", m.get("params", []))
    spec = {**m, "params": params}
    lang = ONLY_IN.get((type_name, name), m.get("lang"))
    title = m["ts"] if m["ts"] == m["py"] else f"{m['ts']} / {m['py']}"
    ts_sig = None if lang == "py" else signature(m["ts"], spec, "ts", method=True)
    py_sig = None if lang == "ts" else signature(m["py"], spec, "py", method=True)
    out = [f"### {title}", ""]
    doc = member_doc(type_name, name, m.get("doc"), lang)
    if doc:
        out += [mdx(doc), ""]
    out.append(code_group(ts_sig, py_sig))
    if params:
        out += ["", param_fields(f"{type_name}.{name}", params)]
    extra = errors_line(m)
    if extra:
        out += ["", extra]
    return "\n".join(out)


def type_page(name: str, t: dict) -> str:
    kind = t["kind"]
    role = t.get("role")
    desc = first_sentence(t.get("doc", "")) or f"The {name} type."
    icon = {"protocol": "Plug", "handle": "Box", "opaque": "Package"}.get(role or "", "Braces")
    body = [frontmatter(name, desc, icon)]
    doc = member_doc(name, None, after_first_sentence(t.get("doc", "")), None)
    if doc:
        body += [mdx(doc), ""]
    labels = {
        "handle": "A handle returned by the library.",
        "protocol": "A protocol: adapters implement it.",
        "opaque": "Opaque: build it with its factory and pass it on; it has no public members.",
    }
    if role in labels:
        body += [f"<Callout>{labels[role]}</Callout>", ""]
    casing = t.get("casing", "api")
    if kind == "alias":
        render = (Render("ts").expr(t["type"]), Render("py").expr(t["type"]))
        body += [code_group(f"type {name} = {render[0]};", f"type {name} = {render[1]}")]
    if kind == "object":
        body += ["## Fields", "", fields_section(name, t["fields"], casing)]
    if kind == "union":
        disc = t["discriminator"]
        body += [f"A union discriminated by `{disc}`.", ""]
        for variant in t["variants"]:
            obj = variant["object"]
            tag = obj.get(disc, {}).get("type", {}).get("literal", "variant")
            body += [f"### {tag}", "", fields_section(name, obj, variant.get("casing", casing)), ""]
    props = {k: v for k, v in t.get("properties", {}).items() if (name, k) not in NOT_BUILT}
    if props:
        body += ["## Properties", "", fields_section(name, props, casing), ""]
    methods = {k: v for k, v in t.get("methods", {}).items() if (name, k) not in NOT_BUILT}
    if methods:
        body += ["## Methods", ""]
        for key, m in methods.items():
            body += [method_section(name, key, m), ""]
    return "\n".join(body).rstrip() + "\n"


def overview_page(api: dict, types_by_group: list[tuple[str, list[str]]]) -> str:
    rows = "\n".join(
        f"| {k} | `{p['ts']}` | `{p['py']}` |" for k, p in api["packages"].items()
    )
    fns = "\n".join(
        f"| [`{f['ts']}`](/docs/reference/functions/{f['ts']}) | `{f['py']}` | "
        f"{mdx(first_sentence(f.get('doc', '')))} |"
        for f in api["functions"].values()
    )
    groups = "\n\n".join(
        f"**{g}:** " + ", ".join(f"[`{n}`](/docs/reference/types/{n})" for n in names)
        for g, names in types_by_group
    )
    return (
        frontmatter(
            "API reference", "Every public function and type, in TypeScript and Python.", "BookOpen"
        )
        + f"""
This reference is generated from `spec/api.json`, the contract both implementations follow. Members
that exist in the contract but are not built yet are left out; members built in one language only
say so.

## Naming

- Functions and methods are `lowerCamelCase` in TypeScript and `snake_case` in Python: `openThread` and `open_thread`.
- Types have the same `PascalCase` name in both.
- Required inputs are positional. Everything else is one trailing options object in TypeScript and keyword-only arguments in Python, with the same names and defaults.
- Expected failures are values, not exceptions: TypeScript returns `{{ ok: true, value }}` or `{{ ok: false, error: {{ code, message }} }}`, Python returns `Ok[T]` or `Err[Failure]`. Only `ConfigError`, for a definition that can't run, is thrown.

## Packages

| Package | TypeScript | Python |
|---|---|---|
{rows}

Model, sandbox, channel and memory providers live in their own packages (`@threads/anthropic`, `threads.anthropic`, ...).
See [Models](/docs/agents/models), [Sandboxes](/docs/sandboxes/overview), [Host server](/docs/host/overview) and [Memory](/docs/memory/memory).

## Functions

| TypeScript | Python | What it does |
|---|---|---|
{fns}

## Types

{groups}

## HTTP API

The host's HTTP API is documented from its OpenAPI file in the [HTTP API reference](/docs/http-api).
"""
    )


# ---------------------------------------------------------------- OpenAPI bundle


def bundle_openapi() -> dict:
    """Inline the cross-file $refs of host-api/openapi.json into components.schemas."""
    host_dir = SPEC / "schema" / "host-api"
    URNS = {}  # $id -> file, so urn: refs resolve like relative ones
    for path in sorted((SPEC / "schema").rglob("*.json")):
        schema = json.loads(path.read_text())
        if isinstance(schema, dict) and isinstance(schema.get("$id"), str):
            URNS[schema["$id"]] = path.resolve()
    openapi = json.loads((host_dir / "openapi.json").read_text())
    files: dict[Path, dict] = {}
    schemas: dict[str, object] = {}
    names: dict[tuple[Path, str], str] = {}

    def load(path: Path) -> dict:
        if path not in files:
            files[path] = json.loads(path.read_text())
        return files[path]

    def resolve(doc: dict, pointer: str) -> object:
        node: object = doc
        for part in pointer.strip("/").split("/"):
            if not isinstance(node, dict):
                raise ValueError(pointer)
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def component(path: Path, pointer: str) -> str:
        if (path, pointer) in names:
            return names[(path, pointer)]
        parts = [p for p in pointer.split("/") if p not in ("", "$defs", "properties")]
        name = "_".join(parts)
        if not path.name.startswith("host-api"):
            name = path.name.split(".")[0] + "_" + name
        names[(path, pointer)] = name
        schemas[name] = None  # reserve before recursing: schemas may be recursive
        schemas[name] = rewrite(resolve(load(path), pointer), path)
        return name

    def rewrite(node: object, base: Path) -> object:
        if isinstance(node, list):
            return [rewrite(x, base) for x in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str) and not v.startswith("#/components"):
                file, _, pointer = v.partition("#")
                if file in URNS:
                    path = URNS[file]
                else:
                    path = (base.parent / file).resolve() if file else base
                if path == (host_dir / "openapi.json").resolve() and not file:
                    out[k] = v
                    continue
                out[k] = f"#/components/schemas/{component(path, pointer)}"
            elif k == "$id":
                continue
            else:
                out[k] = rewrite(v, base)
        return out

    bundled = rewrite(openapi, (host_dir / "openapi.json").resolve())
    assert isinstance(bundled, dict)
    bundled.setdefault("components", {})["schemas"] = dict(sorted(schemas.items()))
    bundled["servers"] = [{"url": DEV_SERVER, "description": "threads dev"}]
    paths = {}
    for route, item in bundled["paths"].items():
        ops = {m: o for m, o in item.items() if not (isinstance(o, dict) and o.get("operationId") in NOT_BUILT_ROUTES)}
        for op in ops.values():
            if isinstance(op, dict) and op.get("operationId") in SUMMARIES:
                op["summary"] = SUMMARIES[op["operationId"]]
        if any(isinstance(o, dict) and "operationId" in o for o in ops.values()):
            paths[route] = ops
    bundled["paths"] = paths
    return bundled


# ---------------------------------------------------------------- main


def outputs() -> dict[Path, str]:
    api = json.loads((SPEC / "api.json").read_text())
    files: dict[Path, str] = {}
    for key, f in api["functions"].items():
        files[REF / "functions" / f"{f['ts']}.mdx"] = function_page(api, key, f)
    grouped = {n for _, names in TYPE_GROUPS for n in names}
    extra = [n for n in api["types"] if n not in grouped]
    type_groups = [(g, [n for n in names if n in api["types"]]) for g, names in TYPE_GROUPS]
    if extra:
        type_groups.append(("Other types", extra))
    for name, t in api["types"].items():
        files[REF / "types" / f"{name}.mdx"] = type_page(name, t)
    files[REF / "overview.mdx"] = overview_page(api, type_groups)
    files[OPENAPI] = json.dumps(bundle_openapi(), indent=2) + "\n"

    # The API Reference sidebar tab: a Fumadocs root folder, grouped by separators.
    pages = [
        "overview",
        "---[SquareFunction]Functions---",
        *(f"functions/{f['ts']}" for f in api["functions"].values()),
    ]
    for g, names in type_groups:
        pages += [f"---[{GROUP_ICONS.get(g, 'Braces')}]{g}---", *(f"types/{n}" for n in names)]
    meta = {
        "title": "API Reference",
        "description": "Functions and types",
        "icon": "SquareCode",
        "root": True,
        "pages": pages,
    }
    files[REF / "meta.json"] = json.dumps(meta, indent=2) + "\n"
    return files


def main() -> int:
    check = "--check" in sys.argv[1:]
    files = outputs()
    stale = [
        p
        for d in (REF / "functions", REF / "types")
        if d.exists()
        for p in d.glob("*.mdx")
        if p not in files
    ]
    changed = [p for p, text in files.items() if not p.exists() or p.read_text() != text]
    if check:
        for p in changed + stale:
            print(f"out of date: {p.relative_to(ROOT)}")
        return 1 if changed or stale else 0
    for p in stale:
        p.unlink()
    for p in changed:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(p_text := files[p])
        del p_text
    print(f"wrote {len(changed)} file(s), removed {len(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Prose cleanup and the MDX building blocks every page uses."""

import json
import re

from .tables import LANG_LABEL


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


def first_sentence(text: str) -> str:
    text = clean(text)
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return (match.group(1) if match else text) or ""


def after_first_sentence(text: str) -> str:
    """The prose after the sentence the page description already shows."""
    return clean(text)[len(first_sentence(text)) :].strip()


def frontmatter(title: str, description: str, icon: str) -> str:
    return (
        f"---\ntitle: {json.dumps(title)}\ndescription: {json.dumps(description)}\n"
        f"icon: {json.dumps(icon)}\n---\n"
    )


def field(attrs: list[str], doc: str) -> str:
    if not doc:
        return f"<Field {' '.join(attrs)} />"
    return f"<Field {' '.join(attrs)}>\n  {mdx(doc)}\n</Field>"


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

# pyright: strict
"""Every mention of `ConfigError` in a scanned adapter source, classified (lane 15's refusal scan).

The scan counts refusals by the constructions it can read, so a scanned source may name
`ConfigError` in only four ways, none of which can carry a construction elsewhere: to construct it
(`ConfigError(`, counted by the scan), to import it under its own name, and to catch it (a Python
`except` clause or `isinstance`, a TypeScript `instanceof`). Every other mention is unclassified
and the scan reports it red: an alias, an assignment, a tuple, a base class, an argument, a type
annotation, a string. Comments and docstrings are ignored. Stdlib only.
"""

from __future__ import annotations

import re

NAME = re.compile(r"\bConfigError\b")
COMMENTS = {
    "py": re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|#[^\n]*'),
    "ts": re.compile(r"/\*[\s\S]*?\*/|//[^\n]*"),
}
# An import that renames it is an alias: left in place, so it stays unclassified.
RENAMED = re.compile(r"\bConfigError\s+as\s+\w+")
IMPORTS = {
    "py": re.compile(r"^[ \t]*from[ \t]+[\w.]+[ \t]+import[ \t]+(?:\([^)]*\)|[^\n]*)", re.M),
    "ts": re.compile(r"\bimport\s+(?:type\s+)?\{[^}]*\}\s*from\s*[\"'][^\"']*[\"']"),
}
ALLOWED = {
    "py": (
        re.compile(r"\bConfigError\("),
        # The clause up to its colon only, so code after `except E:` on one line is still seen.
        re.compile(r"^[ \t]*except\b[^\n:]*:", re.M),
        re.compile(r"\bisinstance\(\s*\w+\s*,\s*ConfigError\s*\)"),
    ),
    "ts": (
        re.compile(r"\bConfigError\("),
        re.compile(r"\binstanceof\s+ConfigError\b"),
    ),
}


def _imports(text: str, lang: str) -> str:
    """Drop imports that bring ConfigError in under its own name."""
    return IMPORTS[lang].sub(lambda m: m.group(0) if RENAMED.search(m.group(0)) else "", text)


def unclassified_uses(source: str, lang: str) -> int:
    """Mentions of ConfigError that are not a construction, a plain import or a catch."""
    text = _imports(COMMENTS[lang].sub("", source), lang)
    for pattern in ALLOWED[lang]:
        text = pattern.sub("", text)
    return len(NAME.findall(text))

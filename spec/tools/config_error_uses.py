# pyright: strict
"""Every mention of `ConfigError` in a scanned adapter source, classified (lane 15's refusal scan).

The scan counts refusals by the constructions it can read, so a scanned source may name
`ConfigError` in only four ways, none of which can carry a construction elsewhere: to construct it
(`ConfigError(`, counted by the scan), to import it under its own name, and to catch it (a Python
`except` clause or `isinstance`, a TypeScript `instanceof`). Every other mention is unclassified
and the scan reports it red: an alias, an assignment, a tuple, a base class, an argument, a type
annotation, a string. Python comments are dropped with the tokenizer, so a `#` inside a
string stays code; TypeScript has no stdlib tokenizer, so nothing is dropped there and a
comment that names ConfigError is red too (write around it). Stdlib only.
"""

from __future__ import annotations

import io
import re
import tokenize

NAME = re.compile(r"\bConfigError\b")
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


def _without_comments(source: str) -> str:
    """Python source with its comments blanked, found by the tokenizer (never inside a string).
    Source the tokenizer can't read is kept whole, so nothing is hidden."""
    lines = source.splitlines(keepends=True)
    try:
        comments = [
            t.start
            for t in tokenize.generate_tokens(io.StringIO(source).readline)
            if t.type == tokenize.COMMENT
        ]
    except (tokenize.TokenError, SyntaxError):
        return source
    for row, col in comments:
        line = lines[row - 1]
        lines[row - 1] = line[:col] + ("\n" if line.endswith("\n") else "")
    return "".join(lines)


def unclassified_uses(source: str, lang: str) -> int:
    """Mentions of ConfigError that are not a construction, a plain import or a catch."""
    text = _imports(_without_comments(source) if lang == "py" else source, lang)
    for pattern in ALLOWED[lang]:
        text = pattern.sub("", text)
    return len(NAME.findall(text))

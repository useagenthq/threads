# pyright: strict
"""Every mention of `ConfigError` and the core helpers that raise one (HELPERS) in a scanned
adapter source, classified (lane 15's refusal scan).

The scan counts refusals by the calls it can read, so a scanned source may name them in only a
few ways, none of which can carry a call elsewhere: to call them (`ConfigError(` with its code as
a string literal, a helper call however qualified; both counted by the scan), to import them
under their own names, and to catch `ConfigError` (a Python `except` clause or `isinstance`, a
TypeScript `instanceof`). Every other mention is unclassified and the scan reports it red: an
alias (any import of a refuser that renames anything), an assignment, a tuple, a base class, an
argument, a type annotation, a string. Python comments are dropped with the tokenizer, so a `#`
inside a string stays code; TypeScript has no stdlib tokenizer, so nothing is dropped there and
a comment that names a refuser is red too (write around it). Stdlib only.
"""

from __future__ import annotations

import io
import re
import tokenize

# The core helpers that raise a ConfigError code for an adapter, by name, and the one pattern for
# a call to each: factory_scan counts these calls and this module allows exactly them, so the
# two can't drift. A call is matched however it is qualified (`threads.secrets.resolve(`); an
# unrelated method of the same name over-counts, which is red, never hidden.
HELPERS: dict[str, dict[str, str]] = {
    "py": {"credential": "missing_secret", "resolve": "missing_secret",
           "check_hosted_tools": "hosted_tool_unsupported"},
    "ts": {"credential": "missing_secret", "reveal": "missing_secret",
           "checkHostedTools": "hosted_tool_unsupported"},
}  # fmt: skip
CALLS = {lang: {h: re.compile(rf"\b{h}\(") for h in helpers} for lang, helpers in HELPERS.items()}
NAMES = {
    lang: re.compile(rf"\b(?:ConfigError|{'|'.join(helpers)})\b")
    for lang, helpers in HELPERS.items()
}
# An import of a refuser that renames anything is kept, so its names stay unclassified (red):
# the scan doesn't try to read alias syntax (`as c`, `as /* note */ c`).
ALIAS = re.compile(r"\bas\b")
IMPORTS = {
    "py": re.compile(r"^[ \t]*from[ \t]+[\w.]+[ \t]+import[ \t]+(?:\([^)]*\)|[^\n]*)", re.M),
    "ts": re.compile(r"\bimport\s+(?:type\s+)?\{[^}]*\}\s*from\s*[\"'][^\"']*[\"']"),
}
CATCHES = {
    "py": (
        # The clause up to its colon only, so code after `except E:` on one line is still seen.
        re.compile(r"^[ \t]*except\b[^\n:]*:", re.M),
        re.compile(r"\bisinstance\(\s*\w+\s*,\s*ConfigError\s*\)"),
    ),
    "ts": (re.compile(r"\binstanceof\s+ConfigError\b"),),
}
CONSTRUCTION = re.compile(r"\bConfigError\(")


def _imports(text: str, lang: str) -> str:
    """Drop imports that bring the refusers in under their own names."""
    names = NAMES[lang]

    def keep(m: re.Match[str]) -> str:
        return m.group(0) if names.search(m.group(0)) and ALIAS.search(m.group(0)) else ""

    return IMPORTS[lang].sub(keep, text)


def without_comments(source: str) -> str:
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
    """Mentions of ConfigError or a refusing helper that are not a call, a plain import or
    (ConfigError only) a catch."""
    text = _imports(without_comments(source) if lang == "py" else source, lang)
    for pattern in (CONSTRUCTION, *CALLS[lang].values(), *CATCHES[lang]):
        text = pattern.sub("", text)
    return len(NAMES[lang].findall(text))

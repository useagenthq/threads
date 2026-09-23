"""`pattern` for semantic rule 20: the portable ECMA-262 subset spec/schema/README.md defines
("Output schemas"), translated to a Python regular expression with the same meaning. The
TypeScript reader (validate/pattern.ts) translates the same subset to ECMAScript, so both answer
the same for every string. Anything outside the subset raises TypeError: refused at setup,
failed closed by a reader.

The meaning is ECMAScript's with the `u` flag, made ASCII-explicit: `\\d` is [0-9], `\\w` is
[A-Za-z0-9_], `\\s` is [\\t\\n\\v\\f\\r ]; `.` is any code point but \\n, \\r, U+2028 and U+2029;
`$` is the end of the string."""

import re
from typing import Final

_SYNTAX: Final = frozenset("\\^$.|?*+()[]{}")
_ESCAPABLE: Final = _SYNTAX | {"/", "-"}
_CLASSES: Final = frozenset("dDwWsS")
_CONTROLS: Final = frozenset("tnvfr")
_DOT: Final = "[^\\n\\r\\u2028\\u2029]"
_BOUND: Final = re.compile(r"\{([0-9]+)(,([0-9]*))?\}")


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """The pattern, compiled with its portable meaning; TypeError outside the subset."""
    try:
        return re.compile(_Translator(pattern).run(), re.ASCII)
    except re.error as error:
        raise TypeError(f"pattern {pattern!r} doesn't compile: {error}") from None


class _Translator:
    """One pass over the pattern. `last` is what a quantifier would apply to: an atom may be
    quantified once, then made lazy once; nothing else may."""

    def __init__(self, pattern: str) -> None:
        self.text = pattern
        self.at = 0
        self.out: list[str] = []
        self.last = "start"
        self.groups: list[str] = []

    def refuse(self, what: str) -> TypeError:
        return TypeError(f"pattern {self.text!r}: {what} is outside the portable subset")

    def run(self) -> str:
        while self.at < len(self.text):
            self.token(self.text[self.at])
        if self.groups:
            raise self.refuse("an unclosed group")
        return "".join(self.out)

    def token(self, char: str) -> None:
        if char in "*+?{":
            self.quantifier(char)
        elif char == "\\":
            self.escape()
        elif char == "[":
            self.klass()
        elif char == "(":
            self.group()
        else:
            self.simple(char)

    def emit(self, text: str, last: str, width: int = 1) -> None:
        self.out.append(text)
        self.last = last
        self.at += width

    def simple(self, char: str) -> None:
        if char in "]}":
            raise self.refuse(f"an unescaped {char!r}")
        if char == ")":
            if not self.groups:
                raise self.refuse("an unbalanced ')'")
            # A lookahead is an assertion: it can't be repeated.
            self.emit(")", "atom" if self.groups.pop() in ("(", "(?:") else "assert")
            return
        special = {".": (_DOT, "atom"), "$": ("\\Z", "assert"), "^": ("^", "assert")}
        text, last = special.get(char, ("|", "start") if char == "|" else (re.escape(char), "atom"))
        self.emit(text, last)

    def quantifier(self, char: str) -> None:
        if char == "?" and self.last == "quantified":
            self.emit("?", "lazy")
            return
        if self.last != "atom":
            raise self.refuse(f"{char!r} after nothing to repeat")
        if char != "{":
            self.emit(char, "quantified")
            return
        bound = _BOUND.match(self.text, self.at)
        if bound is None:
            raise self.refuse("an unescaped '{'")
        self.emit(bound.group(0), "quantified", len(bound.group(0)))

    def group(self) -> None:
        kind = self.text[self.at + 1 : self.at + 3]
        if kind[:1] != "?":
            self.groups.append("(")
            self.emit("(", "start")
        elif kind in ("?:", "?=", "?!"):
            self.groups.append("(" + kind)
            self.emit("(" + kind, "start", 3)
        else:
            raise self.refuse(f"the group '({kind}'")

    def escape(self) -> None:
        char = self.text[self.at + 1 : self.at + 2]
        if char in _CLASSES or char in _CONTROLS:
            self.emit("\\" + char, "atom", 2)
        elif char in ("b", "B"):
            self.emit("\\" + char, "assert", 2)
        elif char and char in _ESCAPABLE:
            self.emit(re.escape(char), "atom", 2)
        else:
            raise self.refuse(f"the escape '\\{char}'")

    def klass(self) -> None:
        """A class: single characters, ranges between two single characters, the escapes
        above but \\S (its complement differs), and '-' as a character only first or last."""
        end = self.at + 1
        parts = ["["]
        if self.text[end : end + 1] == "^":
            parts.append("^")
            end += 1
        start, prev = end, "none"
        while end < len(self.text) and self.text[end] != "]":
            text, width, prev = self.member(end, prev, end == start)
            parts.append(text)
            end += width
        if end >= len(self.text) or end == start:
            raise self.refuse("an empty or unclosed class")
        parts.append("]")
        self.emit("".join(parts), "atom", end + 1 - self.at)

    def member(self, at: int, prev: str, first: bool) -> tuple[str, int, str]:
        """One class member at `at`: its Python text, its width, and what it was."""
        char = self.text[at]
        if char == "\\":
            escaped = self.text[at + 1 : at + 2]
            if escaped in _CLASSES and escaped != "S":
                return "\\" + escaped, 2, "set"
            if escaped and (escaped in _CONTROLS or escaped in _ESCAPABLE):
                return "\\" + escaped, 2, "char"
            raise self.refuse(f"the class escape '\\{escaped}'")
        if char == "[":
            raise self.refuse("an unescaped '[' in a class")
        if char != "-" or first or self.text[at + 1 : at + 2] == "]":
            # The end of a range is not the start of the next one.
            return re.escape(char), 1, "range" if prev == "dash" else "char"
        after = self.text[at + 1 : at + 2]
        if prev != "char" or after == "\\":
            raise self.refuse("a range whose ends are not single characters")
        return "-", 1, "dash"

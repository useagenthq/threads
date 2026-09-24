"""ask_user's matching rule (spec/schema/README.md, "Questions and remembered rules"), written by
hand over code points so both languages agree byte for byte: trim the ECMAScript WhiteSpace and
LineTerminator set (not `str.strip`, which also trims U+001C-U+001F and U+0085), then fold ASCII
A-Z only (not `str.lower` or `casefold`). No Unicode normalization."""

import re
from collections.abc import Sequence
from typing import Final

from pydantic import JsonValue, ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import AskUserInput

type Ask = AskUserInput

_SPACE: Final = frozenset(
    "\u0009\u000a\u000b\u000c\u000d \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_SEPARATOR: Final = re.compile("[,\n\r\u2028\u2029]")
"""The reply separators of a multi_select answer: a comma and the line terminators."""
_DIGITS: Final = re.compile("[0-9]+")


def norm(text: str) -> str:
    """Trimmed of the whitespace set, ASCII letters lowercased."""
    start, end = 0, len(text)
    while start < end and text[start] in _SPACE:
        start += 1
    while end > start and text[end - 1] in _SPACE:
        end -= 1
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text[start:end])


def _options(ask: Ask) -> Sequence[str] | None:
    return None if ask.options is MISSING else ask.options


def _multi(ask: Ask) -> bool:
    return ask.multi_select is not MISSING and ask.multi_select


def ask_problem(ask: Ask) -> str | None:
    """Why an ask_user input can't be asked (rule 46), or None when it can."""
    options = _options(ask)
    if options is None:
        return "multi_select needs options" if _multi(ask) else None
    keys = [norm(o) for o in options]
    if "" in keys:
        return "an option is blank"
    twice = next((i for i, k in enumerate(keys) if keys.index(k) != i), None)
    if twice is not None:
        return f"option {options[twice]} is listed twice"
    split = next((o for o in options if _SEPARATOR.search(o)), None)
    if _multi(ask) and split is not None:
        return f"option {split} contains a comma or a line break"
    return None


def ask_of(value: JsonValue) -> Ask | None:
    """A parsed ask_user input the rules accept, else None."""
    try:
        ask = AskUserInput.model_validate(value)
    except ValidationError:
        return None
    return ask if ask_problem(ask) is None else None


def _numbered(options: Sequence[str]) -> bool:
    """Whether a reply may pick an option by its 1-based number: no option is itself a number."""
    return not any(_DIGITS.fullmatch(norm(o)) for o in options)


def _pick(options: Sequence[str], item: str) -> str | None:
    key = norm(item)
    literal = next((o for o in options if norm(o) == key), None)
    if literal is not None or not _numbered(options) or not _DIGITS.fullmatch(key):
        return literal
    n = int(key)
    return options[n - 1] if 1 <= n <= len(options) else None


def match_answer(ask: Ask, reply: str | Sequence[str]) -> str | None:
    """The recorded answer for a reply, or None when it is none of the options: an option's
    offered spelling, or for multi_select the chosen options in first-reply order joined by
    "\\n". A list is taken item by item; free text is recorded as given when it isn't blank."""
    items = [reply] if isinstance(reply, str) else list(reply)
    options = _options(ask)
    if options is None:
        return None if any(norm(i) == "" for i in items) else "\n".join(items)
    multi = _multi(ask)
    parts = [p for i in items for p in _SEPARATOR.split(i) if norm(p) != ""] if multi else items
    if not parts or (not multi and len(parts) != 1):
        return None
    chosen = [_pick(options, p) for p in parts]
    picked = [c for c in chosen if c is not None]
    if len(picked) != len(chosen):
        return None
    return "\n".join(dict.fromkeys(picked))


def accepts_recorded(ask: Ask, preview: str) -> bool:
    """Whether a recorded answer's text is one the question accepts (rule 25)."""
    options = _options(ask)
    if options is None:
        return True
    parts = preview.split("\n") if _multi(ask) else [preview]
    return all(p in options for p in parts) and len(set(parts)) == len(parts)


def _choices(options: Sequence[str]) -> str:
    """The choices as a message lists them: numbered, or bulleted when an option is a number."""
    numbered = _numbered(options)
    return "\n".join(f"{i}. {o}" if numbered else f"- {o}" for i, o in enumerate(options, 1))


def _hint(ask: Ask, options: Sequence[str]) -> str:
    how = "the number or the text" if _numbered(options) else "the exact text"
    if _multi(ask):
        return f"Reply with {how} of each choice, separated by commas."
    return f"Reply with {how} of your choice."


def question_text(ask: Ask) -> str:
    """The message a channel shows for an open question."""
    options = _options(ask)
    if options is None:
        return ask.question
    return f"{ask.question}\n\n{_choices(options)}\n\n{_hint(ask, options)}"


def correction_text(ask: Ask) -> str:
    """The message a channel shows after a reply that matched no option."""
    options = _options(ask)
    if options is None:
        return "Please answer with some text."
    return f"Please answer with one of:\n\n{_choices(options)}\n\n{_hint(ask, options)}"


def invalid_answer(ask: Ask) -> str:
    """invalid_answer's message: what the question accepts."""
    options = _options(ask)
    return "answer with some text" if options is None else f"answer one of: {', '.join(options)}"

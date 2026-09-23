"""The self-config guard: no agent path
writes config, skills, hooks or schedules. Any non-read-only call with a string argument word
holding a `.threads` path segment is denied before every rule and mode, whatever the tool: the
file tools, bash, MCP, a GitHub edit or a subagent's task."""

import re
from collections.abc import Iterator
from typing import Final

from pydantic import JsonValue

_WORD: Final = re.compile(r"[^\s'\"`;|&<>()=,:]+")
"""Shell quoting, redirection and `--flag=value` never hide a word; failing closed is the aim."""
_SEGMENT: Final = re.compile(r"[/\\]")
CONFIG_DIR: Final = ".threads"


def _strings(value: JsonValue) -> Iterator[str]:
    match value:
        case str():
            yield value
        case list():
            for item in value:
                yield from _strings(item)
        case dict():
            for item in value.values():
                yield from _strings(item)
        case _:
            pass


def touches_config(arguments: JsonValue) -> bool:
    """Whether any argument word names the config directory as a whole path segment."""
    return any(
        CONFIG_DIR in _SEGMENT.split(word)
        for text in _strings(arguments)
        for word in _WORD.findall(text)
    )

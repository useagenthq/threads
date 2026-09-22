"""Permission rules: the `tool` / `tool(specifier)` grammar and its matchers."""

import posixpath
import re
from dataclasses import dataclass
from typing import Literal, assert_never
from urllib.parse import urlsplit

from threads.permissions import shell
from threads.result import Err, Ok

type Family = Literal["bash", "path", "domain", "agent", "plain"]

_RULE = re.compile(r"([a-z][a-z0-9_]{0,127})(\*?)(?:\((.+)\))?", re.DOTALL)
_PATH_TOOLS = frozenset(
    {"read", "write", "edit", "apply_patch", "notebook_edit", "ls", "glob", "grep"}
)
_DOMAIN = re.compile(r"domain:(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)*")
# Glob tokens in match order: `/**/` and a leading `**/` span zero or more directories.
_GLOB = (
    ("/**/", "/(?:.*/)?"),
    ("**/", "(?:.*/)?"),
    ("/**", "/.*"),
    ("**", ".*"),
    ("*", "[^/]*"),
    ("?", "[^/]"),
)


def family(tool: str) -> Family:
    if tool == "bash":
        return "bash"
    if tool in _PATH_TOOLS:
        return "path"
    if tool == "web_fetch" or tool.startswith("browser_"):
        return "domain"
    if tool in ("spawn_agent", "handoff"):
        return "agent"
    return "plain"


@dataclass(frozen=True, slots=True)
class Rule:
    text: str
    tool: str
    wildcard: bool
    """The tool part ended in `*`: it names every tool starting with `tool`."""
    specifier: str | None

    def names(self, tool: str) -> bool:
        return tool.startswith(self.tool) if self.wildcard else tool == self.tool


def parse_rule(text: str) -> Ok[Rule] | Err[str]:
    """A rule the grammar accepts, or why not (setup error permission_rule_invalid)."""
    found = _RULE.fullmatch(text)
    if found is None:
        return Err(f"{text!r} is not tool or tool(specifier)")
    tool, star, specifier = found.group(1), found.group(2), found.group(3)
    rule = Rule(text, tool, star == "*", specifier)
    if specifier is None:
        return Ok(rule)
    if rule.wildcard:
        return Err(f"{text!r}: a tool pattern takes no specifier")
    reason = _specifier_error(family(tool), specifier)
    return Ok(rule) if reason is None else Err(f"{text!r}: {reason}")


def _specifier_error(kind: Family, specifier: str) -> str | None:
    match kind:
        case "bash":
            parsed = shell.parse(specifier.removesuffix(":*"))
            if parsed.unparseable or len(parsed.commands) != 1:
                return "a bash specifier is one plain simple command"
            return None
        case "path" | "agent":
            return None
        case "domain":
            ok = _DOMAIN.fullmatch(specifier) is not None
            return None if ok else "expected domain:host or domain:*.host"
        case "plain":
            return "this tool takes no specifier"
        case _:
            assert_never(kind)


def relative(workspace: str, path: str) -> str | None:
    """The normalized path relative to the workspace root, or None when it lies outside.
    ponytail: `.` and `..` only; symlinks are resolved by the sandbox's realpath, which this
    host-side check doesn't see."""
    root = posixpath.normpath(workspace)
    full = posixpath.normpath(posixpath.join(root, path))
    rel = posixpath.relpath(full, root)
    return None if rel == ".." or rel.startswith("../") else rel


def glob_matches(pattern: str, path: str) -> bool:
    """gitignore(5) semantics: a pattern without an inner `/` matches at any depth, and a
    pattern that matches a directory matches everything under it."""
    dir_only = pattern.endswith("/")
    body = pattern.rstrip("/")
    anchored = "/" in body
    regex = re.compile(("" if anchored else "(?:.*/)?") + _translate(body.removeprefix("/")))
    parts = path.split("/")
    parents = ["/".join(parts[:n]) for n in range(1, len(parts))]
    candidates = parents if dir_only else [*parents, path]
    return any(regex.fullmatch(c) for c in candidates)


def _translate(body: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        token = next((t for t in _GLOB if body.startswith(t[0], i)), None)
        end = body.find("]", i + 2) if body[i] == "[" else -1
        if token is not None:
            out.append(token[1])
            i += len(token[0])
        elif end > 0:
            inner = body[i + 1 : end]
            negated = inner.startswith("!")
            inner = inner.removeprefix("!").replace("\\", "\\\\")
            out.append(f"[{'^' if negated else ''}{inner}]")
            i = end + 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    return "".join(out)


def host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def domain_matches(specifier: str, host: str) -> bool:
    pattern = specifier.removeprefix("domain:")
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return host == pattern

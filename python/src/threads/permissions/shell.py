"""The shell parser behind bash rules.

A command is tokenized with POSIX quoting and split into simple commands on `;`, `&&`, `||`,
`|`, `&`, newlines and the edges of subshells and substitutions (`(`, `)`, backticks). Reserved
words (`if`, `then`, `do`, `!`, ...) that open a simple command are read past, so deny and ask
rules see every command at any depth. It only makes rules conservative: anything it can't read
is flagged unparseable, which can be denied but never allowed. The sandbox stays the security
boundary.
"""

import re
from dataclasses import dataclass

_SEPARATORS = ("&&", "||", ";", "|", "&", "\n")
# Leading assignments that change what the command runs: such a command never matches allow.
_DANGEROUS_ENV = frozenset({"PATH", "BASH_ENV", "ENV", "IFS", "PYTHONPATH", "NODE_OPTIONS", "PS4"})
_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_UNPARSEABLE_WORDS = frozenset({"eval", "exec", "function"})
# Reserved words that can open a simple command without being it.
_KEYWORDS = frozenset(
    {
        "!",
        "{",
        "}",
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "while",
        "until",
        "do",
        "done",
        "esac",
        "coproc",
    }
)
# Unquoted characters that expand, redirect, group or comment: a command holding one never
# matches allow.
_META = frozenset("$<>(){}`#")
# Unquoted characters that open or close a subshell or a substitution: they end a simple command.
_EDGES = frozenset("()`")
_RAW_WORD = re.compile(r"[^\s;&|()`$<>{}\"'\\]+")


@dataclass(frozen=True, slots=True)
class Simple:
    """One simple command after stripping leading assignments and wrappers."""

    words: tuple[str, ...]
    allowable: bool
    """False when a dangerous environment assignment was stripped from it, or when it opens with or
    follows a reserved word."""


@dataclass(frozen=True, slots=True)
class Shell:
    commands: tuple[Simple, ...]
    unparseable: bool
    plain: bool
    """No unquoted metacharacter: no escape, expansion, redirection, group or comment (separators
    don't count). Only a plain command can match an allow rule (it fails closed)."""
    raw_words: tuple[str, ...]
    """The raw text split on whitespace and shell punctuation, for denying input that isn't
    plain."""


def parse(text: str) -> Shell:
    lexer = _Lexer(text)
    tokens, unparseable, meta = lexer.run()
    firsts = [simple(t) for t in tokens]
    unparseable = unparseable or any(_unreadable(c.words) for c in firsts)
    commands = tuple(c for first in firsts for c in _unwrap_keywords(first))
    return Shell(commands, unparseable, not meta, tuple(_RAW_WORD.findall(text)))


def _unwrap_keywords(command: Simple) -> tuple[Simple, ...]:
    """A command that opens with a reserved word, and the command after the word, both as deny
    and ask rules read them. Neither is allowed."""
    if not command.words:
        return ()
    if command.words[0] not in _KEYWORDS:
        return (command,)
    inner = _unwrap_keywords(simple(command.words[1:]))
    return tuple(Simple(c.words, allowable=False) for c in (command, *inner))


def _unreadable(words: tuple[str, ...]) -> bool:
    # Brace groups, function definitions, eval and exec.
    head = words[0] if words else ""
    return head in _UNPARSEABLE_WORDS or "{" in words or "}" in words


def simple(tokens: tuple[str, ...]) -> Simple:
    words = list(tokens)
    allowable = True
    while words:
        assigned = _ASSIGNMENT.match(words[0])
        if assigned is not None:
            name = assigned.group(1)
            allowable = allowable and not _dangerous(name)
            del words[0]
            continue
        wrapped = _wrapper_length(words)
        if wrapped == 0:
            break
        del words[:wrapped]
    return Simple(tuple(words), allowable)


def _dangerous(name: str) -> bool:
    return name in _DANGEROUS_ENV or name.startswith(("LD_", "DYLD_"))


def _wrapper_length(words: list[str]) -> int:
    """How many leading words are a `timeout N`, `nice`, `nohup` or `time` wrapper."""
    head = words[0]
    if head in ("nohup", "time"):
        return 1
    if head not in ("timeout", "nice"):
        return 0
    n = 1
    while n < len(words) and words[n].startswith("-"):
        # -s SIGNAL, -k DURATION and nice's -n N take a separate argument.
        n += 2 if words[n] in ("-s", "-k", "-n") else 1
    return n + 1 if head == "timeout" else n


class _Lexer:
    def __init__(self, text: str) -> None:
        self._text = text
        self._i = 0
        self._word: list[str] = []
        self._has_word = False
        self._tokens: list[str] = []
        self._commands: list[tuple[str, ...]] = []
        self._unparseable = False
        self._meta = False

    def run(self) -> tuple[list[tuple[str, ...]], bool, bool]:
        while self._i < len(self._text):
            self._step(self._text[self._i])
        self._end_command()
        return self._commands, self._unparseable, self._meta

    def _step(self, c: str) -> None:
        if c in " \t":
            self._end_word()
            self._i += 1
        elif c == "'":
            self._single()
        elif c == '"':
            self._double()
        elif c == "\\":
            self._meta = True
            self._take(self._text[self._i + 1 : self._i + 2].replace("\n", ""))
            self._i += 2
        elif c in _EDGES:
            # Subshells, function definitions, command and process substitution: the command
            # inside is read on its own.
            self._unparseable = self._meta = True
            self._end_command()
            self._i += 1
        elif not self._separator():
            if self._text.startswith("<<", self._i):
                self._unparseable = True  # a here-doc
            self._meta = self._meta or c in _META
            self._take(c)
            self._i += 1

    def _separator(self) -> bool:
        for sep in _SEPARATORS:
            if self._text.startswith(sep, self._i):
                self._end_command()
                self._i += len(sep)
                return True
        return False

    def _single(self) -> None:
        end = self._text.find("'", self._i + 1)
        if end < 0:
            self._unparseable = True
            end = len(self._text)
        self._take(self._text[self._i + 1 : end])
        self._i = end + 1

    def _double(self) -> None:
        self._i += 1
        self._has_word = True
        while self._i < len(self._text) and self._text[self._i] != '"':
            c = self._text[self._i]
            if c == "`" or self._text.startswith("$(", self._i):
                self._unparseable = True
            self._meta = self._meta or c in "\\$`"
            if c == "\\" and self._text[self._i + 1 : self._i + 2] in ('"', "\\", "$", "`"):
                self._i += 1
                c = self._text[self._i]
            self._take(c)
            self._i += 1
        if self._i >= len(self._text):
            self._unparseable = True
        self._i += 1

    def _take(self, chars: str) -> None:
        self._word.append(chars)
        self._has_word = True

    def _end_word(self) -> None:
        if self._has_word:
            self._tokens.append("".join(self._word))
        self._word, self._has_word = [], False

    def _end_command(self) -> None:
        self._end_word()
        self._commands.append(tuple(self._tokens))
        self._tokens = []

# pyright: strict
"""Shell compound and nested-command policy cases (ADR 0022 amendment 2026-09-24)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .policies import permissions
from .policy import Row, write_policy_case

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

STATUS, NPM, RM, PUSH = "bash(git status:*)", "bash(npm test)", "bash(rm:*)", "bash(git push:*)"
D = "default"


def _perms(*allow: str) -> Obj:
    return {**permissions(D), "allow": list(allow), "ask": [PUSH], "deny": [RM]}


def _sh(command: str, decision: str, source: str, rule: str = "", mode: str = D) -> Row:
    return (mode, "bash", "other", {"command": command}, decision, source, rule)


def _compound(root: pathlib.Path) -> None:
    rows: list[Row] = [
        _sh("git status && npm test", "allow", "policy", STATUS),
        _sh("git status | npm test", "allow", "policy", STATUS),
        _sh("npm test; git status -s", "allow", "policy", NPM),
        _sh("npm test || git status", "allow", "policy", NPM),
        _sh("npm test & git status", "allow", "policy", NPM),
        _sh("npm test\ngit status", "allow", "policy", NPM),
        _sh("git status && FOO=1 npm test", "allow", "policy", STATUS),
        _sh("git status && ls", "ask", "mode"),
        _sh("git status && PATH=/x npm test", "ask", "mode"),
        _sh("git status && rm -rf build", "deny", "policy", RM),
        _sh("npm test | git push origin", "ask", "policy", PUSH),
        _sh("git push origin; rm -rf build", "deny", "policy", RM),
        _sh("git status && echo $(id)", "ask", "mode"),
        _sh("git status > out.txt && npm test", "ask", "mode"),
        _sh("git status && npm test 'unclosed", "ask", "mode"),
        _sh("git status && ls", "deny", "mode", mode="dont_ask"),
    ]
    write_policy_case(
        root,
        "permission-rule-bash-compound",
        "A command joined with &&, ||, ;, |, & or a newline is allowed only when every simple "
        "command matches an allow rule; the first command's rule is reported. One unmatched "
        "part asks, one part matching an ask rule asks, and one part matching a deny rule "
        "denies the whole command. A redirection, expansion or unclosed quote anywhere asks.",
        {"permissions": _perms(STATUS, NPM)},
        rows,
    )
    rows = [
        _sh("npm test | tail", "allow", "policy", "bash"),
        _sh("npm test && git status", "allow", "policy", "bash"),
        _sh("npm test | rm -rf build", "deny", "policy", RM),
        _sh("npm test | tail > out.txt", "ask", "mode"),
    ]
    write_policy_case(
        root,
        "permission-rule-bash-bare-compound",
        "A bare bash allow rule matches every simple command, so it allows a compound of them. "
        "A deny rule still denies any part, and a redirection still asks.",
        {"permissions": _perms("bash")},
        rows,
    )


def _nested(root: pathlib.Path) -> None:
    rows: list[Row] = [
        _sh("if true; then rm -rf /; fi", "deny", "policy", RM),
        _sh("if rm -rf /; then true; fi", "deny", "policy", RM),
        _sh("if true; then true; else rm -rf /; fi", "deny", "policy", RM),
        _sh("if true; then true; elif rm -rf /; then true; fi", "deny", "policy", RM),
        _sh("while true; do rm -rf /; done", "deny", "policy", RM),
        _sh("until npm test; do rm -rf a; done", "deny", "policy", RM),
        _sh("for f in a b; do rm -rf a; done", "deny", "policy", RM),
        _sh("! rm -rf /", "deny", "policy", RM),
        _sh("coproc rm -rf /", "deny", "policy", RM),
        _sh("(rm -rf /)", "deny", "policy", RM),
        _sh("(r''m -rf /)", "deny", "policy", RM),
        _sh("{ r''m -rf /; }", "deny", "policy", RM),
        _sh("true && (cd a && r''m -rf /)", "deny", "policy", RM),
        _sh("echo $(r''m -rf /)", "deny", "policy", RM),
        _sh("echo `r''m -rf /`", "deny", "policy", RM),
        _sh("f() { rm -rf /; }", "deny", "policy", RM),
        _sh("case a in a) rm -rf /;; esac", "deny", "policy", RM),
        _sh("if true; then git push origin; fi", "ask", "policy", PUSH),
        # A command inside a control structure or group is seen, but never allowed.
        _sh("if true; then npm test; fi", "ask", "mode"),
        _sh("(npm test)", "ask", "mode"),
        _sh("{ npm test; }", "ask", "mode"),
        _sh("! npm test", "ask", "mode"),
        # A string another shell runs is not looked into, so nothing allows it.
        _sh("bash -c 'rm -rf /'", "ask", "mode"),
    ]
    write_policy_case(
        root,
        "permission-rule-bash-nested-deny",
        "Deny and ask rules see every simple command the parser finds at any depth: inside "
        "if, while, until, for and case, after ! and coproc, and in subshells, brace groups, "
        "function bodies and command substitutions. A command inside one of these is never "
        "allowed. A string another shell runs (bash -c) is not looked into, so it asks.",
        {"permissions": _perms(STATUS, NPM, "bash(true)")},
        rows,
    )


def build(root: pathlib.Path) -> None:
    _compound(root)
    _nested(root)

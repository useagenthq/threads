"""The permission fold: the first decisive step wins.

The self-config guard, deny rules, plan mode, protected paths, ask rules, allow rules, then the
mode default. In `dont_ask` every ask from the later steps becomes deny. Hooks and the
principal's authority fold in afterwards, in the loop; nothing here can turn a deny into an
allow.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import PermissionMode, PermissionRuleAddedData, Permissions
from threads.permissions import rules, shell
from threads.permissions.rules import Rule, family, parse_rule
from threads.permissions.self_config import touches_config
from threads.result import Ok

type Category = Literal["read_only", "edit", "other"]
type Verdict = Literal["allow", "ask", "deny"]
type Source = Literal[
    "policy", "hook", "default", "self_config_guard", "mode", "protected_path", "thread_rule"
]
"""PermissionDecisionData.source; a test pins it to the generated schema type."""

_PLAN_TOOLS = frozenset({"ask_user", "exit_plan_mode", "todo_write"})
# Columns: read_only inside the workspace, read_only outside, edits inside, everything else.
_MODE_TABLE: Mapping[PermissionMode, tuple[Verdict, Verdict, Verdict, Verdict]] = {
    "plan": ("allow", "ask", "deny", "deny"),
    "dont_ask": ("allow", "deny", "deny", "deny"),
    "default": ("allow", "ask", "ask", "ask"),
    "accept_edits": ("allow", "ask", "allow", "ask"),
    "bypass": ("allow", "allow", "allow", "allow"),
}
_RANK: Mapping[Verdict, int] = {"allow": 0, "ask": 1, "deny": 2}


@dataclass(frozen=True, slots=True)
class Decision:
    decision: Verdict
    source: Source
    rule: str | None = None
    """The matched rule string, when a rule decided."""
    reason: str | None = None
    """A hook's why (the reason of its deny, the rule of its ask), or memory_write's."""


@dataclass(frozen=True, slots=True)
class _Call:
    tool: str
    category: Category
    path: str | None
    """Workspace-relative and normalized; None when absent or outside the workspace."""
    outside: bool
    host: str | None
    agent: str | None
    command: shell.Shell | None
    """Set exactly for bash. A missing command is unparseable."""


@dataclass(frozen=True, slots=True)
class Call:
    """The call being decided: its tool, the category standing in for the tool's class, and
    its arguments (parsed by the tool's input schema before this, but read defensively)."""

    tool: str
    category: Category
    input: JsonValue


def decide(
    permissions: Permissions,
    workspace: str,
    mode: PermissionMode,
    call: Call,
    *,
    thread_rules: Sequence[PermissionRuleAddedData] = (),
) -> Decision:
    if call.category != "read_only" and touches_config(call.input):
        return Decision("deny", "self_config_guard")
    decided = _fold(permissions, mode, _prepare(workspace, call), thread_rules)
    if mode == "dont_ask" and decided.decision == "ask":
        return Decision("deny", decided.source, decided.rule)
    return decided


def decide_capped(
    target: Permissions, ceiling: Permissions, workspace: str, mode: PermissionMode, call: Call
) -> Decision:
    """A handoff target's decision intersected with the principal and host ceiling, decided in
    the ceiling's own mode. The stricter wins; on a tie, the target's."""
    mine = decide(target, workspace, mode, call)
    cap = decide(ceiling, workspace, ceiling.mode, call)
    return cap if _RANK[cap.decision] > _RANK[mine.decision] else mine


def _fold(
    p: Permissions,
    mode: PermissionMode,
    call: _Call,
    thread_rules: Sequence[PermissionRuleAddedData],
) -> Decision:
    denied = _deny_step(p, call, thread_rules)
    if denied is not None:
        return denied
    gated = _gate_step(p, mode, call)
    if gated is not None:
        return gated
    asked = _first_any(_parsed(p.ask), call)
    if asked is not None:
        return Decision("ask", "policy", asked)
    allowed = _allowed(_parsed(p.allow), call)
    if allowed is not None:
        return Decision("allow", "policy", allowed)
    thread_allowed = _allowed(_thread(thread_rules, "allow"), call)
    if thread_allowed is not None:
        return Decision("allow", "thread_rule", thread_allowed)
    return Decision(_MODE_TABLE[mode][_column(call)], "mode")


def _gate_step(p: Permissions, mode: PermissionMode, call: _Call) -> Decision | None:
    """Plan mode, then protected paths: both hold even where an allow rule matches."""
    if mode == "plan" and call.category != "read_only":
        return Decision("allow" if call.tool in _PLAN_TOOLS else "deny", "mode")
    path = call.path
    protected = path is not None and any(rules.glob_matches(g, path) for g in p.protected_paths)
    return Decision("ask", "protected_path") if call.category == "edit" and protected else None


def _deny_step(
    p: Permissions, call: _Call, thread_rules: Sequence[PermissionRuleAddedData]
) -> Decision | None:
    denied = _first_any(_parsed(p.deny), call)
    if denied is not None:
        return Decision("deny", "policy", denied)
    thread_denied = _first_any(_thread(thread_rules, "deny"), call)
    return None if thread_denied is None else Decision("deny", "thread_rule", thread_denied)


def _thread(
    thread_rules: Sequence[PermissionRuleAddedData], decision: Literal["allow", "deny"]
) -> list[Rule]:
    return _parsed(r.rule for r in thread_rules if r.decision == decision)


def _column(call: _Call) -> int:
    if call.category == "read_only":
        return 1 if call.outside else 0
    return 2 if call.category == "edit" and not call.outside else 3


def _parsed(texts: Iterable[str]) -> list[Rule]:
    # Setup rejects invalid rules (permission_rule_invalid); one that slips through never matches.
    return [r.value for r in map(parse_rule, texts) if isinstance(r, Ok)]


def _first_any(candidates: Sequence[Rule], call: _Call) -> str | None:
    """Deny and ask: any simple command matching is enough."""
    return next((r.text for r in candidates if _any_hit(r, call)), None)


def _allowed(candidates: Sequence[Rule], call: _Call) -> str | None:
    """Allow: for bash, only a plain single simple command can match (fail closed: separators,
    escapes, expansions and redirections never do), and never one with a dangerous leading
    assignment. `bash(*)` is the one exception: it matches every command."""
    parsed = call.command
    if parsed is None:
        return next((r.text for r in candidates if _hit(r, call)), None)
    anything = next((r.text for r in candidates if _any_command(r)), None)
    if anything is not None:
        return anything
    if parsed.unparseable or not parsed.plain:
        return None
    first: str | None = None
    for command in parsed.commands:
        match = next((r.text for r in candidates if _bash_hit(r, command)), None)
        if match is None or not command.allowable:
            return None
        first = first or match
    return first


def _any_hit(rule: Rule, call: _Call) -> bool:
    parsed = call.command
    if parsed is None:
        return _hit(rule, call)
    if _any_command(rule) or any(_bash_hit(rule, c) for c in parsed.commands):
        return True
    # Unparseable input: the rule's leading words appearing anywhere in the raw text deny it.
    return parsed.unparseable and rule.names("bash") and _consecutive(rule, parsed.raw_words)


def _any_command(rule: Rule) -> bool:
    """`bash(*)`: every command, parsed or not; the sandbox is the boundary."""
    return rule.names("bash") and rule.specifier == "*"


def _bash_hit(rule: Rule, command: shell.Simple) -> bool:
    if not rule.names("bash"):
        return False
    if rule.specifier is None:
        return True
    words = _rule_words(rule.specifier)
    if rule.specifier.endswith(":*"):
        return command.words[: len(words)] == words
    return command.words == words


def _rule_words(specifier: str) -> tuple[str, ...]:
    commands = shell.parse(specifier.removesuffix(":*")).commands
    return commands[0].words if commands else ()


def _consecutive(rule: Rule, raw: tuple[str, ...]) -> bool:
    if rule.specifier is None:
        return True
    words = _rule_words(rule.specifier)
    n = len(words)
    return n > 0 and any(raw[i : i + n] == words for i in range(len(raw) - n + 1))


def _hit(rule: Rule, call: _Call) -> bool:
    if not rule.names(call.tool):
        return False
    spec = rule.specifier
    if spec is None:
        return True
    kind = family(call.tool)
    if kind == "path":
        return call.path is not None and rules.glob_matches(spec, call.path)
    if kind == "domain":
        return call.host is not None and rules.domain_matches(spec, call.host)
    return kind == "agent" and call.agent == spec


def _prepare(workspace: str, call: Call) -> _Call:
    tool, fields = call.tool, call.input if isinstance(call.input, dict) else {}
    path, url, agent, command = (
        v if isinstance(v := fields.get(k), str) else None
        for k in ("path", "url", "agent", "command")
    )
    rel = None if path is None else rules.relative(workspace, path)
    host = None if url is None else rules.host_of(url)
    parsed = None
    if family(tool) == "bash":
        parsed = shell.parse(command) if command is not None else shell.Shell((), True, False, ())
    return _Call(tool, call.category, rel, path is not None and rel is None, host, agent, parsed)

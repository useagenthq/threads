"""`ConfigError` (spec/api.json): the one failure that raises, only at setup."""

from dataclasses import dataclass
from typing import Literal

type ConfigErrorCode = Literal[
    "invalid_config",
    "missing_secret",
    "unknown_preset",
    "duplicate_name",
    "capability_missing",
    "mcp_unreachable",
    "budget_unenforceable",
    "permission_rule_invalid",
    "hosted_tool_unsupported",
    "egress_policy_unsupported",
    "transport_fence_unsupported",
    "handoff_in_team",
]


class ConfigError(Exception):
    """A definition that can't run. Every other expected failure is a value."""

    def __init__(self, code: ConfigErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code: ConfigErrorCode = code
        self.message = message


class UnboundError(ConfigError):
    """A dynamic member's recorded choice names a tool or model its template no longer has here:
    its rebind fails pin_unavailable, unlike a setup that failed for now."""


@dataclass(frozen=True, slots=True)
class Failure:
    """A setup failure as a value (spec/api.json conventions.results): what `Agent.check`
    returns where a run raises `ConfigError`."""

    code: ConfigErrorCode
    message: str

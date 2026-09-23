"""An agent's definition and what it pins at thread start: `thread_started`."""

from dataclasses import dataclass

from pydantic import JsonValue

from threads.agents.bindings import AppTool
from threads.log import Budget, Permissions, Retry
from threads.log.digest import canonical_sha256
from threads.loop.model import Model
from threads.reduce.handlers import to_json
from threads.result import Ok


@dataclass(frozen=True, slots=True)
class Definition[D]:
    name: str
    model: Model
    instructions: str
    tools: tuple[AppTool[D], ...]
    permissions: Permissions | None = None
    budget: Budget | None = None
    retry: Retry | None = None

    def policy(self) -> dict[str, JsonValue]:
        """The resolved runtime policy: each section absent (ADR defaults) or complete."""
        pinned: dict[str, JsonValue] = {"models": [to_json(self.model.info.limits)]}
        if self.permissions is not None:
            pinned["permissions"] = to_json(self.permissions)
        if self.budget is not None:
            pinned["budget"] = to_json(self.budget)
        if self.retry is not None:
            pinned["retry"] = to_json(self.retry)
        return pinned

    def thread_started(self) -> dict[str, JsonValue]:
        """The pinned, secret-free config. Everything model-visible in it is line 0."""
        info = self.model.info
        data: dict[str, JsonValue] = {
            "agent_name": self.name,
            "instructions": self.instructions,
            "model": to_json(info.model),
            "model_params": dict(info.params),
            "adapter": to_json(info.adapter),
            "tools": [to_json(t.spec()) for t in self.tools],
            "policy": self.policy(),
        }
        digest = canonical_sha256(data)
        if not isinstance(digest, Ok):
            raise AssertionError("a definition built from parsed models always canonicalizes")
        return {**data, "config_hash": digest.value}

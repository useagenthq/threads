"""An agent's definition and what it pins at thread start: `thread_started`."""

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.agents.bindings import AppTool, ToolServer
from threads.agents.builtins import Egress, egress_denied
from threads.agents.catalog import NO_CATALOG, Catalog
from threads.hooks.extension import Extension, extension_tools
from threads.log import Budget, Context, Permissions, Principal, Retry, ToolSpec
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.loop.calls import FINAL_OUTPUT
from threads.loop.model import Model
from threads.memory.authority import MemoryWrite
from threads.memory.protocol import KnowledgeProvider, MemoryProvider
from threads.memory.setup import writes
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.sandbox.protocol import Sandbox
from threads.tools import specs
from threads.tools.specs import agent_tools


@dataclass(frozen=True, slots=True)
class Definition[D]:
    name: str
    model: Model
    instructions: str
    tools: tuple[AppTool[D], ...]
    permissions: Permissions | None = None
    budget: Budget | None = None
    retry: Retry | None = None
    context: Context | None = None
    sandbox: Sandbox | None = None
    egress: Egress = ()
    extensions: tuple[Extension, ...] = ()
    memory: MemoryProvider | None = None
    memory_write: MemoryWrite = "ask"
    knowledge: KnowledgeProvider | None = None
    servers: tuple[ToolServer, ...] = ()
    """Tool servers (MCP); their tools join `tools` when a run connects them."""
    subagents: "tuple[Definition[None], ...]" = ()
    """What spawn_agent may start, by name."""
    handoffs: "tuple[Definition[None], ...]" = ()
    """What handoff may hand the conversation to, pinned as policy.handoffs (item 2)."""
    member: bool = False
    """Started as a subagent: a team member, offered the team tools (item 3)."""
    allowed: frozenset[str] | None = None
    """A subagent's tools are its own filtered to these, its parent's pinned names: a child only
    narrows. final_output is exempt."""
    catalog: Catalog = NO_CATALOG
    """The capabilities configured: web, git, computer use and lsp."""
    on_unknown_usage: Literal["upper_bound", "stop"] | None = None
    """Pinned as policy.on_unknown_usage when set; "stop" lets a limit with no per-attempt bound
    through setup, refused at run time instead."""
    approvers: tuple[Principal, ...] | None = None
    """Who may answer this agent's approval challenges. None: the local
    operator for a run, nobody for a channel thread. Host policy, never pinned."""

    def policy(self) -> dict[str, JsonValue]:
        """The resolved runtime policy: each section absent (ADR defaults) or complete."""
        pinned: dict[str, JsonValue] = {"models": [to_json(self.model.info.limits)]}
        if self.permissions is not None:
            pinned["permissions"] = to_json(self.permissions)
        if self.budget is not None:
            pinned["budget"] = to_json(self.budget)
        if self.on_unknown_usage is not None:
            pinned["on_unknown_usage"] = self.on_unknown_usage
        if self.retry is not None:
            pinned["retry"] = to_json(self.retry)
        if self.context is not None:
            pinned["context"] = to_json(self.context)
        if self.handoffs:
            pinned["handoffs"] = [h.name for h in self.handoffs]
        return pinned

    def specs(self) -> tuple[ToolSpec, ...]:
        """Built-ins sorted by name (read_tool_result always, the sandbox tools with a
        sandbox, memory and knowledge tools with a provider, framework tools as offered), then
        app tools in declared order and MCP tools sorted by name, then extension tools sorted by
        namespaced name."""
        builtins = specs(
            sandbox=self.sandbox is not None,
            egress_denied=egress_denied(self.egress),
            memory=writes(self.memory),
            knowledge=self.knowledge is not None,
            framework=agent_tools(
                spawn=bool(self.subagents),
                team=bool(self.subagents) or self.member,
                handoffs=bool(self.handoffs),
            ),
            gated=self.catalog.gated(),
        )
        ext = extension_tools(self.extensions)
        mine = (*builtins, *(t.spec() for t in self.tools), *(t.spec() for t in ext))
        allowed = self.allowed
        if allowed is None:
            return mine
        return tuple(s for s in mine if s.name in allowed or s.name == FINAL_OUTPUT)

    @property
    def full_instructions(self) -> str:
        """Base instructions, then each extension's, in declaration order, then the agents this
        one may start and hand off to: all line 0, so pinned per thread (C7)."""
        parts = [self.instructions, *(e.instructions for e in self.extensions)]
        if self.subagents:
            names = ", ".join(a.name for a in self.subagents)
            parts.append(f"Subagents you can start with spawn_agent: {names}.")
        if self.handoffs:
            names = ", ".join(a.name for a in self.handoffs)
            parts.append(f"Agents you can hand the conversation to: {names}.")
        return "\n\n".join(p for p in parts if p)

    def pin(self) -> tuple[dict[str, JsonValue], bytes]:
        """`thread_started` and the canonical bytes of the resolved, secret-free config its
        `config_hash` names. The config adds each extension's hook and observer manifest, so the
        host content-addressed store holds exactly which hooks a thread runs."""
        info = self.model.info
        data: dict[str, JsonValue] = {
            "agent_name": self.name,
            "instructions": self.full_instructions,
            "model": to_json(info.model),
            "model_params": dict(info.params),
            "adapter": to_json(info.adapter),
            "tools": [to_json(t) for t in self.specs()],
            "policy": self.policy(),
        }
        if self.sandbox is not None:
            data["sandbox_provider"] = self.sandbox.info.provider
        config: dict[str, JsonValue] = dict(data)
        if self.extensions:
            config["extensions"] = self.manifests()
        if self.memory is not None:
            # Write authority is config: a changed policy is a changed pin.
            config["memory_write"] = self.memory_write
        if self.subagents:
            # What a child may do is part of what this thread may do: a changed subagent is a
            # changed config.
            config["subagents"] = [s.pin()[0]["config_hash"] for s in self.subagents]
        text = canonicalize(config)
        if not isinstance(text, Ok):
            raise AssertionError("a definition built from parsed models always canonicalizes")
        raw = text.value.encode("utf-8")
        return {**data, "config_hash": sha256_hex(raw)}, raw

    def manifests(self) -> list[JsonValue]:
        return [e.manifest() for e in self.extensions]

    def thread_started(self) -> dict[str, JsonValue]:
        """The pinned, secret-free config. Everything model-visible in it is line 0."""
        return self.pin()[0]

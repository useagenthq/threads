"""An agent's definition and what it pins at thread start: `thread_started`."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.bindings import AppTool, ToolServer
from threads.agents.builtins import Egress, egress_denied
from threads.agents.cache_ttl import agreed_cache_ttl
from threads.agents.catalog import NO_CATALOG, Catalog
from threads.agents.skills import Skill, listing, pinned
from threads.agents.tool import Tool, json_schema
from threads.hooks.extension import Extension, extension_tools
from threads.log import Budget, Context, Permissions, Principal, Retry, ToolSpec
from threads.log.digest import canonical_sha256, sha256_hex
from threads.log.jcs import canonicalize
from threads.loop.defaults import CONTEXT
from threads.loop.model import Model
from threads.loop.output import FINAL_OUTPUT
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
    """What handoff may hand the conversation to, pinned as policy.handoffs."""
    member: bool = False
    """Started as a subagent: a team member, offered the team tools."""
    allowed: frozenset[str] | None = None
    """A subagent's tools are its own filtered to these, its parent's pinned names: a child only
    narrows. final_output is exempt."""
    catalog: Catalog = NO_CATALOG
    skills: tuple[Skill, ...] = ()
    """Host-pinned skills; keyword only, like catalog)."""
    """the capabilities configured: web, git, computer use and lsp."""
    on_unknown_usage: Literal["upper_bound", "stop"] | None = None
    """Pinned as policy.on_unknown_usage when set; "stop" lets a limit with no per-attempt bound
    through setup, refused at run time instead."""
    approvers: tuple[Principal, ...] | None = None
    """Who may answer this agent's approval challenges. None: the local
    operator for a run, nobody for a channel thread. Host policy, never pinned."""
    output: type[BaseModel] | None = None
    """The structured final output, taken through final_output; None: the final text."""
    output_retries: int = 2
    """Failed candidates per turn before the turn ends output_invalid."""
    fallback: tuple[Model, ...] = ()
    """Models to fall back to, in order, when the current one stays overloaded."""
    output_styles: tuple[tuple[str, str], ...] = ()
    """Named instructions Thread.set_output_style switches to, pinned as policy.output_styles."""

    def policy(self) -> dict[str, JsonValue]:
        """The resolved runtime policy: each section absent (ADR defaults) or complete. An
        agent without output or fallback pins no section for them, so its config is unchanged."""
        pinned: dict[str, JsonValue] = {"models": self._models()}
        if any(m.info.limits.price is not MISSING for m in (self.model, *self.fallback)):
            # Prices are nano-USD (spec/schema/README.md); without a currency cost() is None.
            pinned["currency"] = "USD"
        if self.fallback:
            pinned["fallback"] = [_settings(m) for m in self.fallback]
        if self.output is not None:
            pinned["output"] = _output_policy(self.output, self.output_retries)
        sections = (
            ("permissions", self.permissions),
            ("budget", self.budget),
            ("retry", self.retry),
            ("context", self._context()),
        )
        pinned |= {name: to_json(value) for name, value in sections if value is not None}
        if self.on_unknown_usage is not None:
            pinned["on_unknown_usage"] = self.on_unknown_usage
        if self.handoffs:
            pinned["handoffs"] = [h.name for h in self.handoffs]
        if self.output_styles:
            pinned["output_styles"] = dict(self.output_styles)
        return pinned

    def _context(self) -> Context | None:
        """The given context; else, when the models agree on a cache TTL other than the default,
        the default context with it. An all-5m agent pins what it always did."""
        if self.context is not None:
            return self.context
        ttl = agreed_cache_ttl((self.model, *self.fallback))
        if ttl is None or ttl == CONTEXT.cache_ttl_ms:
            return None
        return CONTEXT.model_copy(update={"cache_ttl_ms": ttl})

    def _models(self) -> list[JsonValue]:
        """Every model this thread may use, primary first, once per (provider, name)."""
        limits: dict[tuple[str, str], JsonValue] = {}
        for model in (self.model, *self.fallback):
            info = model.info.limits
            limits.setdefault((info.provider, info.name), to_json(info))
        return list(limits.values())

    def specs(self) -> tuple[ToolSpec, ...]:
        """Built-ins sorted by name (read_tool_result always, the sandbox tools with a
        sandbox, memory and knowledge tools with a provider, framework tools as offered), then
        app tools in declared order and MCP tools sorted by name, then extension tools sorted by
        namespaced name. final_output comes last, after a subagent's narrowing."""
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
            skills=bool(self.skills),
        )
        ext = extension_tools(self.extensions)
        mine = (*builtins, *(t.spec() for t in self.tools), *(t.spec() for t in ext))
        allowed = self.allowed
        if allowed is not None:
            mine = tuple(s for s in mine if s.name in allowed)
        return mine if self.output is None else (*mine, _final_output(self.output))

    @property
    def full_instructions(self) -> str:
        """Base instructions, then each extension's, in declaration order, then the skill listing,
        then the agents this one may start and hand off to: all line 0, so pinned per thread
        (C7)."""
        parts = [self.instructions, *(e.instructions for e in self.extensions)]
        parts.append(listing(self.skills))
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
        if self.skills:
            config["skills"] = pinned(self.skills)
        if self.memory is not None:
            # Write authority is config: a changed policy is a changed pin.
            config["memory_write"] = self.memory_write
        if self.subagents:
            # What a child may do is part of what this thread may do: a changed subagent is a
            # changed config.
            config["subagents"] = [s.pin()[0]["config_hash"] for s in self.subagents]
        if concurrent := self.concurrent_tools():
            # Changes when reads run, not what the model sees: hashed, not in line 0.
            config["concurrent_tools"] = list[JsonValue](sorted(concurrent))
        text = canonicalize(config)
        if not isinstance(text, Ok):
            raise AssertionError("a definition built from parsed models always canonicalizes")
        raw = text.value.encode("utf-8")
        return {**data, "config_hash": sha256_hex(raw)}, raw

    def concurrent_tools(self) -> frozenset[str]:
        """The pinned app and extension tools declared `concurrent=True`: the loop's grouping
        lookup, hashed as `concurrent_tools`. MCP and built-in tools are never concurrent."""
        pinned = {s.name for s in self.specs()}
        mine = [(t.name, t) for t in self.tools]
        mine += [(t.name, t.tool) for t in extension_tools(self.extensions)]
        return frozenset(
            name for name, t in mine if name in pinned and isinstance(t, Tool) and t.concurrent
        )

    def manifests(self) -> list[JsonValue]:
        return [e.manifest() for e in self.extensions]

    def thread_started(self) -> dict[str, JsonValue]:
        """The pinned, secret-free config. Everything model-visible in it is line 0."""
        return self.pin()[0]


def _settings(model: Model) -> dict[str, JsonValue]:
    """A fallback's settings epoch; like the primary's, its reasoning carries over as recorded."""
    info = model.info
    return {
        "model": to_json(info.model),
        "model_params": dict(info.params),
        "adapter": to_json(info.adapter),
        "reasoning_carryover": "keep",
    }


def _output_policy(output: type[BaseModel], retries: int) -> dict[str, JsonValue]:
    """policy.output: the model's schema and its hash. native is reserved on the wire; v0.1
    offers tool mode only."""
    schema = json_schema(output)
    digest = canonical_sha256(schema)
    if not isinstance(digest, Ok):
        raise AssertionError("a Pydantic JSON Schema always canonicalizes")
    return {"schema": schema, "schema_sha256": digest.value, "mode": "tool", "max_retries": retries}


def _final_output(output: type[BaseModel]) -> ToolSpec:
    """Tool mode: final_output takes the output schema and ends the turn."""
    data: dict[str, JsonValue] = {
        "name": FINAL_OUTPUT,
        "description": "Return the final structured result.",
        "input_schema": json_schema(output),
        "effect_class": "read_only",
        "ends_turn": True,
    }
    return ToolSpec.model_validate(data)

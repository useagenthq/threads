"""An agent's definition and what it pins at thread start: `thread_started`."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.bindings import DEFAULT_PERMISSIONS, AppTool, ToolServer
from threads.agents.builtins import Egress, egress_denied
from threads.agents.cache_ttl import agreed_cache_ttl
from threads.agents.catalog import NO_CATALOG, Catalog
from threads.agents.config import ConfigError
from threads.agents.deferral import DeferTools, Pinned, pinned_tools
from threads.agents.instructions import full_instructions
from threads.agents.skills import Skill, pinned
from threads.agents.tool import Tool, json_schema
from threads.hooks.extension import Extension, extension_tools
from threads.log import (
    Budget,
    Context,
    MemberDefine,
    Permissions,
    Principal,
    Retry,
    ToolSpec,
    WorkspacePin,
)
from threads.log.digest import canonical_sha256, sha256_hex
from threads.log.jcs import canonicalize
from threads.loop.defaults import CONTEXT, RETRY
from threads.loop.model import Model
from threads.loop.output import FINAL_OUTPUT
from threads.memory.authority import MemoryWrite
from threads.memory.protocol import KnowledgeProvider, MemoryProvider
from threads.memory.setup import writes
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.sandbox.protocol import Sandbox
from threads.team.ops import TeamLimits
from threads.team.policy import MessagePolicyRule, team_tools
from threads.tools import specs
from threads.tools.specs import agent_tools
from threads.workspace import Workspace


@dataclass(frozen=True, slots=True)
class Dynamic:
    """A dynamic member's choice: its template, the define its starter chose, and the starter
    (the lead's name, or operator), whose name marks the written block."""

    template: str
    define: MemberDefine
    starter: str


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
    extensions: "tuple[Extension[D], ...]" = ()
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
    """Started as a subagent: a task-board member, offered its tools."""
    team: "tuple[Definition[None], ...] | None" = None
    """agent(team=...): the agents start may name. None: no team."""
    rules: tuple[MessagePolicyRule, ...] = ()
    """The host message_policy rules with this agent as `from` (lane 29C): they add the agents it
    may start and, without a team of its own, the only team tools it is offered."""
    rule_agents: "tuple[Definition[None], ...]" = ()
    """The host agents `rules` name, since a rule-only lead's own team is empty."""
    team_limits: TeamLimits = field(default_factory=TeamLimits)
    in_team: bool = False
    """Pinned as a team's member: offered the team tools."""
    answerer: bool = False
    """A host answers for the run (a channel conversation or an HTTP API call): ask_user is
    offered. Never for agent.run(), a schedule or a subagent."""
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
    context_set: frozenset[str] | None = None
    """The context fields a partial context names; None: a complete context sets them all. A
    field left out takes what an omitted context would (cache_ttl_ms, defer_tools)."""
    models: tuple[tuple[str, Model], ...] = ()
    """dynamic_agent(models=...): a template's model keys in order, the first the default (and
    `model`). Empty: not a template."""
    dynamic: Dynamic | None = None
    """A dynamic member: pinned with this choice, which the hashed config records."""
    inherited_defer: DeferTools | None = None
    """A child's parent's resolved defer_tools: pinned when the child sets no context."""
    workspace: Workspace | None = None
    """agent(workspace=...): what every sandbox this thread creates starts with in /workspace.
    Needs a sandbox; the host resolves it once, after setup, into `workspace_pin`."""
    workspace_pin: WorkspacePin | None = None
    """`workspace` resolved on the host into one tree, pinned as policy.workspace."""

    def policy(self) -> dict[str, JsonValue]:
        """The resolved runtime policy. Permissions, retry and context are always pinned
        complete, defaults included (spec/schema/README.md, "The pinned config"); an agent
        without output, budget or fallback pins no section for them."""
        pinned: dict[str, JsonValue] = {"models": self._models()}
        if any(m.info.limits.price is not MISSING for m in (self.model, *self.fallback)):
            # Prices are nano-USD (spec/schema/README.md); without a currency cost() is None.
            pinned["currency"] = "USD"
        if self.fallback:
            pinned["fallback"] = [_settings(m) for m in self.fallback]
        if self.output is not None:
            pinned["output"] = _output_policy(self.output, self.output_retries)
        sections = (
            ("permissions", self.permissions or DEFAULT_PERMISSIONS),
            ("budget", self.budget),
            ("retry", self.retry or RETRY),
            ("context", self._context()),
        )
        pinned |= {name: to_json(value) for name, value in sections if value is not None}
        if self.on_unknown_usage is not None:
            pinned["on_unknown_usage"] = self.on_unknown_usage
        if self.handoffs:
            pinned["handoffs"] = [h.name for h in self.handoffs]
        if self.output_styles:
            pinned["output_styles"] = dict(self.output_styles)
        if self.workspace_pin is not None:
            pinned["workspace"] = to_json(self.workspace_pin)
        return pinned

    def _context(self) -> Context:
        """The given context, else the default, with what the agent didn't set itself filled in:
        cache_ttl_ms from the models' agreed lifetime and defer_tools from a parent."""
        base = self.context or CONTEXT
        update: dict[str, object] = {}
        ttl = None if self._sets("cache_ttl_ms") else agreed_cache_ttl((self.model, *self.fallback))
        if ttl is not None:
            update["cache_ttl_ms"] = ttl
        inherited = None if self._sets("defer_tools") else self.inherited_defer
        if inherited is not None:
            update["defer_tools"] = inherited
        return base.model_copy(update=update) if update else base

    def _sets(self, field: str) -> bool:
        """The agent set this context field itself: a complete context sets every field."""
        given = self.context_set
        return self.context is not None and (given is None or field in given)

    def defer_tools(self) -> DeferTools:
        """The resolved defer_tools: what this agent pins, and what its children inherit."""
        if self.context is not None and self._sets("defer_tools"):
            return self.context.defer_tools
        return self.inherited_defer or CONTEXT.defer_tools

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
                members=team_tools(self.team, self.in_team, self.rules),
                answerer=self.answerer and not self.member,
            ),
            gated=self.catalog.gated(),
            skills=bool(self.skills),
        )
        user = self._user_tools()
        allowed = self.allowed
        mine = (*builtins, *user.specs)
        if allowed is not None:
            mine = tuple(s for s in mine if s.name in allowed)
        if user.search is not None:
            # A child's own tool_search is exempt from its parent's names: loads are per thread.
            mine = _with_search(mine, user.search, {b.name for b in builtins})
        return mine if self.output is None else (*mine, _final_output(self.output))

    def _user_tools(self) -> Pinned:
        ext = extension_tools(self.extensions)
        pairs = [(t, t.spec()) for t in self.tools] + [(t, t.spec()) for t in ext]
        return pinned_tools(pairs, self.defer_tools())

    def spec_artifacts(self) -> tuple[bytes, ...]:
        """The deferred tools' full specs, each put before the thread_started naming it."""
        return self._user_tools().artifacts

    @property
    def full_instructions(self) -> str:
        """Line 0's instructions (agents/instructions.py)."""
        return full_instructions(self)

    def _check_workspace(self) -> None:
        """A workspace needs a sandbox, and is pinned only once a run or check() resolved it."""
        if self.workspace is None:
            return
        if self.sandbox is None:
            raise ConfigError(
                "capability_missing", "workspace: needs a sandbox to place the files in"
            )
        if self.workspace_pin is None:
            raise ConfigError(
                "invalid_config",
                "workspace: its files are read on the host by check() or a run; "
                "this pin can't see them",
            )

    def pin(self) -> tuple[dict[str, JsonValue], bytes]:
        """`thread_started` and the canonical bytes of the resolved, secret-free config its
        `config_hash` names. The config adds each extension's hook and observer manifest, so the
        host content-addressed store holds exactly which hooks a thread runs."""
        self._check_workspace()
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
        if self.sandbox is not None:
            # A resumed run must match these, so a changed one is a changed config.
            info = self.sandbox.info
            config["sandbox"] = {
                "provider": info.provider,
                "egress": info.egress,
                "capture_classes": list[JsonValue](info.capture_classes),
                "policy": self.egress if isinstance(self.egress, str) else list(self.egress),
            }
        if self.extensions:
            config["extensions"] = self.manifests()
        if self.skills:
            config["skills"] = pinned(self.skills)
        if self.memory is not None:
            # Write authority is config: a changed policy is a changed pin.
            config["memory_write"] = self.memory_write
        if self.dynamic is not None:
            # The choice is config: two keys naming one model still pin differently.
            define = to_json(self.dynamic.define)
            config["dynamic"] = {"template": self.dynamic.template, "define": define}
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


def _with_search(
    specs: tuple[ToolSpec, ...], search: ToolSpec, builtins: set[str]
) -> tuple[ToolSpec, ...]:
    """tool_search sorted by name among the built-ins, which lead the specs."""
    at = next(
        (i for i, s in enumerate(specs) if s.name not in builtins or s.name > search.name),
        len(specs),
    )
    return (*specs[:at], search, *specs[at:])


@dataclass(frozen=True, slots=True)
class DryPin:
    """An agent pinned without setup (spec lane 22, B.3), and what it therefore couldn't see."""

    started: Mapping[str, JsonValue]
    """The thread_started data a new thread would pin, MCP tools left out."""
    mcp: tuple[str, ...]
    """MCP servers whose tools only a connection lists."""
    setup_extensions: tuple[str, ...]
    """Extensions with a setup step: their tools and instructions may come from it."""
    setup_providers: tuple[Literal["memory", "knowledge"], ...]
    """Memory and knowledge providers with a setup step, by role."""
    leads_team: bool
    """agent(team=...): its members run in the team worker, outside the run tree."""


def dry_pin[D](definition: Definition[D]) -> DryPin:
    """The agent's thread_started as a new thread would pin it, without running any setup: it
    resolves no secret, opens no MCP connection and runs no extension or provider setup."""
    providers: list[Literal["memory", "knowledge"]] = []
    # SetsUp (agents/setup.py) imports this module; its one method is what counts.
    if callable(getattr(definition.memory, "setup", None)):
        providers.append("memory")
    if callable(getattr(definition.knowledge, "setup", None)):
        providers.append("knowledge")
    return DryPin(
        definition.pin()[0],
        tuple(s.name for s in definition.servers),
        tuple(e.name for e in definition.extensions if e.setup is not None),
        tuple(providers),
        definition.team is not None,
    )


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

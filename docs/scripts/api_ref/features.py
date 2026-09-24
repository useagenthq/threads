"""The "Python and TypeScript" page: which features each language has, read from the contract.

Each row names the contract members that make up a feature. A member counts in a language when
spec/api.json lists it there and spec/api-surface-gaps.json doesn't mark it missing there. Provider
factories not in the contract yet (spec/api-surface-factory-decisions.json) count when their
adapter's source exists and doesn't refuse at setup. A name that is in none of these files fails the
generator, so the table can't drift from the spec.
"""

from .json_access import Obj, array, load, obj, text
from .tables import ROOT, SPEC
from .text import frontmatter

type Row = tuple[str, str, tuple[str, ...], str]  # label, link, refs, note

# Factories whose adapter exists in a language but refuses at setup, so it can't be used there.
REFUSED = frozenset({("modal", "ts"), ("mem0", "ts"), ("mem0", "py")})

AREAS: tuple[tuple[str, tuple[Row, ...]], ...] = (
    (
        "Agents and tools",
        (
            ("Agents and runs", "/docs/agents/agents", ("agent", "Agent.run"), ""),
            ("Streaming", "/docs/agents/agents", ("Agent.stream",), ""),
            ("Blocking run from sync code", "/docs/agents/agents", ("Agent.runSync",), ""),
            ("Custom tools", "/docs/agents/tools", ("tool",), ""),
            ("Checked tool results", "/docs/agents/tools", ("tool.output",), ""),
            ("Structured output", "/docs/agents/structured-output", ("agent.output",), ""),
            (
                "Built-in web fetch and search",
                "/docs/agents/built-in-tools",
                ("agent.web", "exa"),
                "",
            ),
            ("Skills", "/docs/agents/skills", ("agent.skills",), ""),
            ("MCP servers", "/docs/agents/mcp", ("mcp",), ""),
        ),
    ),
    (
        "Models",
        (
            ("Anthropic", "/docs/agents/models", ("anthropic",), ""),
            ("OpenAI", "/docs/agents/models", ("openai",), ""),
            (
                "Any other provider",
                "/docs/agents/models",
                ("ts:factory:aiSdk", "py:factory:litellm"),
                "TypeScript through the AI SDK, Python through LiteLLM's OpenAI-compatible route.",
            ),
            ("Fallback models", "/docs/agents/models", ("agent.fallback",), ""),
        ),
    ),
    (
        "Multi-agent",
        (
            (
                "Subagents",
                "/docs/multi-agent/subagents",
                ("agent.subagents",),
                "In TypeScript a subagent has no sandbox.",
            ),
            ("Handoffs", "/docs/multi-agent/handoffs", ("agent.handoffs",), ""),
            (
                "Team handle: start, ask and wait on members from code",
                "/docs/multi-agent/teams",
                ("openTeam", "Team"),
                "",
            ),
            (
                "Message rules between agents",
                "/docs/multi-agent/teams",
                ("host.message_policy",),
                "",
            ),
        ),
    ),
    (
        "Sandboxes",
        (
            ("E2B", "/docs/sandboxes/e2b", ("factory:e2b",), "TypeScript runs it on Bun."),
            ("Daytona", "/docs/sandboxes/daytona", ("factory:daytona",), ""),
            ("Modal", "/docs/sandboxes/modal", ("factory:modal",), ""),
            ("Fake sandbox for tests", "/docs/evals/testing", ("fakeSandbox",), ""),
        ),
    ),
    (
        "Channels and host",
        (
            (
                "Host server and HTTP API",
                "/docs/host/http-api",
                ("host", "Host.startRun", "Host.subscribe"),
                "",
            ),
            ("Slack", "/docs/host/slack", ("factory:slack",), ""),
            ("WhatsApp", "/docs/host/whatsapp", ("factory:whatsapp",), ""),
            ("GitHub", "/docs/host/github", ("factory:github",), ""),
            ("Cron schedules", "/docs/host/schedules", ("host.schedules",), ""),
        ),
    ),
    (
        "Memory and knowledge",
        (
            ("Local memory", "/docs/memory/memory", ("localMemory",), ""),
            ("Supermemory", "/docs/memory/memory", ("supermemory",), ""),
            ("Zep", "/docs/memory/memory", ("zep",), ""),
            ("Mem0", "/docs/memory/memory", ("factory:mem0",), ""),
            ("Local knowledge base", "/docs/memory/knowledge", ("localKnowledge",), ""),
        ),
    ),
    (
        "Control",
        (
            ("Hooks", "/docs/control/hooks", ("extension",), ""),
            (
                "Permissions and approvals",
                "/docs/control/permissions",
                ("agent.permissions", "Thread.approve"),
                "",
            ),
            ("Budgets", "/docs/control/budgets", ("agent.budget",), ""),
            ("Usage and cost", "/docs/control/usage-and-cost", ("Thread.usage", "Thread.cost"), ""),
        ),
    ),
    (
        "Evals and testing",
        (
            ("Timeline", "/docs/evals/timeline", ("openThread", "Thread.timeline"), ""),
            ("Replay", "/docs/evals/replay", ("Thread.replay",), ""),
            (
                "Fork",
                "/docs/evals/fork",
                ("Thread.fork", "Thread.forkPoints"),
                "Stub forks (no live side effects) are Python only.",
            ),
            ("Saved cases", "/docs/evals/saved-cases", ("Thread.saveCase",), ""),
            ("Scripted model", "/docs/evals/testing", ("scriptedModel",), ""),
        ),
    ),
)

INTRO = """What works in each language today. The table is built from the API contract both
languages are tested against, so it changes when they do.

**✓** works today. **Not yet** is planned for that language. **—** doesn't apply to that language.
"""


class Surface:
    """What each language has, from the contract, the gap list and the adapter sources."""

    def __init__(self) -> None:
        self.api = obj(load(SPEC / "api.json"))
        self.factories = obj(obj(load(SPEC / "api-surface-factory-decisions.json"))["factories"])
        self.missing = {
            (text(g["name"]), text(g["lang"]))
            for g in (obj(e) for e in array(load(SPEC / "api-surface-gaps.json")))
            if text(g["kind"]) == "missing"
        }

    def node(self, ref: str) -> Obj:
        """The api.json node a ref names: a function, a function param, a type or a type member."""
        head, _, member = ref.partition(".")
        fns, types = obj(self.api["functions"]), obj(self.api["types"])
        if head in fns:
            f = obj(fns[head])
            if not member:
                return f
            for p in (obj(p) for p in array(f["params"])):
                if text(p["name"]) == member:
                    return p
        elif head in types:
            t = obj(types[head])
            if not member:
                return t
            for section in ("methods", "properties", "fields"):
                if member in obj(t.get(section, {})):
                    return obj(obj(t[section])[member])
        raise SystemExit(f"features.py: {ref} is not in spec/api.json")

    def factory_has(self, name: str, lang: str) -> bool:
        if name not in self.factories:
            raise SystemExit(f"features.py: {name} is not in api-surface-factory-decisions.json")
        kebab = "".join(f"-{c.lower()}" if c.isupper() else c for c in name)
        src = (
            ROOT / "typescript" / "packages" / kebab / "src"
            if lang == "ts"
            else ROOT / "python" / "src" / "threads" / f"{kebab.replace('-', '_')}.py"
        )
        return src.exists() and (name, lang) not in REFUSED

    def status(self, ref: str, lang: str) -> str:
        """ "yes", "no", or "n/a" when the ref is one language's by design."""
        prefix, _, rest = ref.partition(":")
        if prefix in ("ts", "py"):
            return self.status(rest, lang) if prefix == lang else "skip"
        if prefix == "factory":
            return "yes" if self.factory_has(rest, lang) else "no"
        if self.node(ref).get("lang") not in (None, lang):
            return "n/a"
        parts = ref.split(".")
        gap = any((".".join(parts[:i]), lang) in self.missing for i in range(1, len(parts) + 1))
        return "no" if gap else "yes"

    def cell(self, refs: tuple[str, ...], lang: str) -> str:
        found = {self.status(r, lang) for r in refs} - {"skip"}
        if "n/a" in found:
            return "—"
        return "✓" if found == {"yes"} else "Not yet"


def _section(surface: Surface, area: str, rows: tuple[Row, ...]) -> str:
    notes = any(note for *_, note in rows)
    head = "| Feature | TypeScript | Python |" + (" Notes |" if notes else "")
    rule = "|---|:-:|:-:|" + ("---|" if notes else "")
    lines = [
        f"| [{label}]({link}) | {surface.cell(refs, 'ts')} | {surface.cell(refs, 'py')} |"
        + (f" {note} |" if notes else "")
        for label, link, refs, note in rows
    ]
    return "\n".join([f"## {area}", "", head, rule, *lines])


def features_page() -> str:
    surface = Surface()
    sections = [_section(surface, area, rows) for area, rows in AREAS]
    title = frontmatter(
        "Python and TypeScript",
        "Which features each language has today, feature by feature.",
        "Languages",
    )
    generated = (
        "{/* Generated by docs/scripts/gen_api_ref.py from spec/. Do not edit by hand. */}\n"
    )
    return title + "\n" + generated + "\n" + INTRO + "\n" + "\n\n".join(sections) + "\n"

"""Paths and the hand-kept tables that adjust what spec/api.json says for readers.

spec/api.json is the contract, and it also lists members that are not built yet. Those are
kept out of the docs by NOT_BUILT, and members built in one language only are marked by
ONLY_IN. Update both tables when a member lands.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "spec"
DOCS = ROOT / "docs"
REF = DOCS / "content" / "docs" / "reference"
OPENAPI = DOCS / "openapi.json"

type Member = tuple[str, str | None]

# (container, member): in spec/api.json, not in either implementation yet.
NOT_BUILT: frozenset[Member] = frozenset(
    {
        ("agent", "output_mode"),
        ("agent", "browser"),
        ("agent", "output_styles"),
        ("agent", "stream_release"),
        ("tool", "module"),
        ("tool", "entrypoint"),
        ("tool", "concurrent"),
        ("Thread", "replay"),
        ("Thread", "compact"),
        ("Thread", "setOutputStyle"),
        # Exists, but no tool asks the user a question yet (ask_user is not built).
        ("Thread", "answer"),
    }
)

# HTTP routes left out for the same reason, by operationId.
NOT_BUILT_ROUTES = frozenset({"answerQuestion"})

# Titles of the generated HTTP pages, by operationId.
SUMMARIES = {
    "startRun": "Start a run",
    "subscribeRun": "Stream a run's events",
    "getTimeline": "Get a thread's timeline",
    "listBranches": "List branches",
    "listForkPoints": "List fork points",
    "fork": "Fork a thread",
    "listApprovals": "List pending approvals",
    "decideApproval": "Approve or deny",
    "resolveParked": "Resolve a parked action",
    "cancel": "Cancel a thread",
    "setModel": "Change the model",
    "setMode": "Change the permission mode",
    "channelChallenge": "Channel verification challenge",
    "channelWebhook": "Channel webhook",
}

# The address `threads dev` serves on, so the playground can build example requests.
DEV_SERVER = "http://localhost:8787"

# (container, member): built in one language only.
ONLY_IN: dict[Member, str] = {
    ("agent", "output"): "ts",
    ("agent", "output_retries"): "ts",
    ("agent", "fallback"): "ts",
    ("agent", "on_unknown_usage"): "py",
    ("tool", "output"): "ts",
    ("Agent", "check"): "ts",
}

# Extra sentences where today's behavior is narrower than the contract.
NOTES: dict[Member, str] = {
    ("tool", "runs"): 'Only "host" is supported today; "sandbox" is a setup error.',
    ("mcp", "runs"): 'Only "host" is supported today; "sandbox" is a setup error.',
    ("agent", "egress"): "Host allowlists are not supported yet in either language: use [] "
    '(deny-all) or "unenforced".',
    ("Agent", "run"): "Python: an agent with app tools needs deps on every run; "
    "pass deps=None when its tools take none.",
    ("Thread", "forkPoints"): "TypeScript returns the list itself; Python returns Ok or Err.",
    ("Thread", "todos"): "TypeScript returns the list itself; Python returns Ok or Err.",
    ("Thread", "children"): "TypeScript returns the list itself; Python returns Ok or Err.",
    ("RunResult", None): "In TypeScript, thread is a ThreadRef (id, branch, store): pass it to "
    "openThread for the full Thread handle. In Python it is the Thread handle.",
}

# Contract text that only makes sense next to the spec, rewritten for readers.
DOC_OVERRIDES: dict[Member, str] = {
    ("Thread.fork", "mode"): "stub runs the new branch without live side effects: every "
    "mediated call is answered from what the original branch recorded after the fork point. "
    "Stubbing works in Python only today; in TypeScript a run on a stub fork is live. A "
    "stub-mode run on a live model that declares hosted tools is refused with ConfigError "
    "hosted_tool_unsupported, because hosted calls can't be stubbed.",
}

# Python spellings that differ from the contract's inline shape.
PY_TYPES: dict[Member, str] = {("tool", "reconcile"): "Reconcile[Output, Deps]"}

LANG_LABEL = {"ts": "TypeScript", "py": "Python"}

TYPE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Agents and runs",
        (
            "Agent",
            "RunResult",
            "RunStream",
            "RunContext",
            "Tool",
            "McpServer",
            "Extension",
            "Hooks",
            "Skill",
            "Secret",
            "Store",
            "ConfigError",
            "ConfigErrorCode",
        ),
    ),
    ("Threads and evals", ("Thread", "CaseExpectation", "SavedCase")),
    ("Host", ("Host", "Schedule")),
    (
        "Model adapters",
        (
            "Model",
            "ModelContext",
            "ModelInfo",
            "ModelRequest",
            "ModelResponse",
            "ModelChunk",
            "LookupResult",
            "LookupCapability",
        ),
    ),
    (
        "Sandbox adapters",
        (
            "Sandbox",
            "SandboxAuthority",
            "SandboxContext",
            "SandboxInfo",
            "SandboxSession",
            "ExecOutput",
            "ExecResult",
        ),
    ),
    (
        "Memory and knowledge adapters",
        (
            "MemoryProvider",
            "KnowledgeProvider",
            "Scope",
            "Binding",
            "MemoryRecord",
            "RecordRef",
            "MemoryHit",
            "KnowledgeSource",
            "DocVersion",
            "KnowledgeHit",
            "Doc",
            "SearchBackend",
            "SearchHit",
        ),
    ),
    (
        "Channel adapters",
        (
            "ChannelAdapter",
            "ChannelCapabilities",
            "RawRequest",
            "RawResponse",
            "VerifiedDelivery",
            "Inbound",
            "DeliveryOutcome",
        ),
    ),
)

# Sidebar separator icons (lucide names, as Fumadocs resolves them).
GROUP_ICONS = {
    "Agents and runs": "Bot",
    "Threads and evals": "RotateCcwClock",
    "Host": "Server",
    "Model adapters": "Cpu",
    "Sandbox adapters": "Box",
    "Memory and knowledge adapters": "Brain",
    "Channel adapters": "MessageCircle",
}

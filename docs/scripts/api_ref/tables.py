"""Paths, what is built, and the hand-kept tables that adjust what spec/api.json says for readers.

spec/api.json is the contract, and it also lists members that are not built yet. Which ones is
read from spec/api-surface-gaps.json, the one registry the API surface gate keeps honest: a
member missing in both languages is left out of the docs (NOT_BUILT), and one missing in a single
language is marked as built in the other (ONLY_IN).
"""

from pathlib import Path

from .json_access import array, load, obj, text

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "spec"
DOCS = ROOT / "docs"
REF = DOCS / "content" / "docs" / "reference"
OPENAPI = DOCS / "openapi.json"

type Member = tuple[str, str | None]


def _missing() -> dict[Member, set[str]]:
    """(container, member) of each missing gap, with the languages it is missing from."""
    gaps = [obj(entry) for entry in array(load(SPEC / "api-surface-gaps.json"))]
    # An optional method's missing gap in Python is its capability protocol's (not exported
    # yet), not the method's, so the docs neither hide it nor mark it one-language.
    capabilities = {
        f"{type_name}.{name}"
        for type_name, t in obj(obj(load(SPEC / "api.json"))["types"]).items()
        for name, m in obj(obj(t).get("methods", {})).items()
        if "capability" in obj(m)
    }
    out: dict[Member, set[str]] = {}
    for gap in gaps:
        if text(gap["kind"]) == "missing" and text(gap["name"]) not in capabilities:
            container, _, name = text(gap["name"]).rpartition(".")
            out.setdefault((container, name), set()).add(text(gap["lang"]))
    return out


_MISSING = _missing()
NOT_BUILT: frozenset[Member] = frozenset(m for m, ls in _MISSING.items() if ls == {"ts", "py"})
ONLY_IN: dict[Member, str] = {
    m: "py" if langs == {"ts"} else "ts" for m, langs in _MISSING.items() if len(langs) == 1
}

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

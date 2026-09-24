"""The configured capabilities (spec/api.json agent options `web`, `git`, `computer`,
`lsp`): which gated built-ins are pinned. Naming one whose capability is missing is a setup
error that names it."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Required, TypedDict

from threads.agents.config import ConfigError
from threads.git.forge import GitHub
from threads.git.gateway import Forge, GitGateway
from threads.log import ArtifactRef
from threads.loop.tools import ToolRunner
from threads.sandbox.protocol import Sandbox
from threads.secrets import Secret, resolve
from threads.store import SqliteStore
from threads.tools.lsp import LANGUAGES
from threads.tools.runner import SandboxTools
from threads.tools.specs import GIT
from threads.web.http import Fence
from threads.web.search import SearchBackend
from threads.web.tools import WebTools


class WebOptions(TypedDict, total=False):
    fetch: bool
    """web_fetch; default False."""
    search: SearchBackend
    """web_search through this backend (`threads.search`: exa, brave, tavily)."""


class GitOptions(TypedDict, total=False):
    credential: Required[Secret]
    """The forge token, revealed on the host only."""
    forge_url: str
    """Where owner/name lives: <forge_url>/<owner>/<name>.git. Default https://github.com."""
    api_url: str
    """The forge REST API open_pull_request uses. Default https://api.github.com."""


class LspOptions(TypedDict, total=False):
    languages: Required[Sequence[str]]
    """typescript, python, go or rust; the sandbox image carries their servers."""


@dataclass(frozen=True, slots=True)
class Catalog:
    fetch: bool = False
    search: SearchBackend | None = None
    git: Secret | None = None
    computer: bool = False
    languages: tuple[str, ...] = ()
    forge: Forge = field(default_factory=Forge)
    """Where git_* and open_pull_request reach the forge."""

    def gated(self) -> frozenset[str]:
        """The gated built-ins this configuration pins."""
        names: set[str] = set()
        if self.fetch:
            names.add("web_fetch")
        if self.search is not None:
            names.add("web_search")
        if self.git is not None:
            names |= GIT
        if self.computer:
            names |= {"computer", "computer_screenshot"}
        if self.languages:
            names.add("lsp")
        return frozenset(names)

    def servers(self) -> dict[str, tuple[str, ...]]:
        """The declared languages' server commands."""
        return {lang: LANGUAGES[lang] for lang in self.languages}


NO_CATALOG: Final = Catalog()


def catalog(
    sandbox: Sandbox | None,
    *,
    web: WebOptions | None,
    git: GitOptions | None,
    computer: bool,
    lsp: LspOptions | None,
) -> Catalog:
    """Raises ConfigError: capability_missing for a tool the sandbox can't host, unknown_preset
    for an lsp language threads has no server for."""
    languages = tuple(lsp["languages"]) if lsp is not None else ()
    unknown = [lang for lang in languages if lang not in LANGUAGES]
    if unknown:
        raise ConfigError("unknown_preset", f"lsp languages {unknown}: known are {list(LANGUAGES)}")
    if computer and (sandbox is None or sandbox.info.desktop == "none"):
        raise ConfigError("capability_missing", "computer needs a sandbox with a desktop")
    if languages and sandbox is None:
        raise ConfigError("capability_missing", "lsp runs in a sandbox: configure one")
    if git is not None and sandbox is None:
        raise ConfigError("capability_missing", "the git gateway clones into a sandbox")
    web = web or WebOptions()
    return Catalog(
        web.get("fetch", False),
        web.get("search"),
        None if git is None else git["credential"],
        computer,
        languages,
        Forge() if git is None else _forge(git),
    )


def _forge(git: GitOptions) -> Forge:
    base = git.get("forge_url", "https://github.com").rstrip("/")
    return Forge(
        git_url=lambda repo: f"{base}/{repo}.git",
        api=GitHub(api=git.get("api_url", "https://api.github.com")),
    )


def gateways(
    configured: Catalog, sandbox: SandboxTools | None, store: SqliteStore, fence: Fence
) -> dict[str, ToolRunner]:
    """The host gateway runners by tool name. Secrets are resolved here, at run setup."""
    out: dict[str, ToolRunner] = {}
    if configured.fetch or configured.search is not None:

        async def put(data: bytes, media_type: str) -> ArtifactRef:
            sha = await store.put_artifact(data)
            return ArtifactRef(sha256=sha, bytes=len(data), media_type=media_type)

        web = WebTools(fence, put, configured.search)
        out |= dict.fromkeys(("web_fetch", "web_search"), web)
    if configured.git is not None and sandbox is not None:
        gateway = GitGateway(resolve(configured.git), sandbox, fence, configured.forge)
        out |= dict.fromkeys(GIT, gateway)
    return out

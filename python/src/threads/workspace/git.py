"""The host's git for workspace inputs: whether a directory is in a work tree, what that tree
ignores (git decides, so neither language reimplements gitignore), its submodules, and a
checkout of a forge repository. A local directory's git runs with the user's own environment,
so the global excludes count (spec/schema/README.md, Workspace inputs)."""

import os
import re
import tempfile
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from threads.agents.config import ConfigError
from threads.git.host import Git, credential_env, git
from threads.redaction.text import redact_secrets

_REPO: Final = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_REF: Final = re.compile(r"^[A-Za-z0-9._/-]+$")
_NO_GIT: Final = 127
"""git's exit code when it can't be started at all."""


def invalid(message: str) -> ConfigError:
    return ConfigError("invalid_config", redact_secrets(message))


async def _local(directory: Path, *args: str) -> Git:
    """The user's git in `directory`; exit code 127 when git can't be started."""
    try:
        return await git(args, directory, os.environ)
    except (FileNotFoundError, PermissionError) as e:
        return Git(_NO_GIT, "", str(e))


def _dot_git_above(directory: Path) -> bool:
    """A `.git` in `directory` or any parent: a work tree as seen without git."""
    at = directory.resolve()
    return any((parent / ".git").exists() for parent in (at, *at.parents))


async def in_work_tree(directory: Path, label: str) -> bool:
    """Whether `directory` is inside a git work tree. With no git on the host, a `.git` in it or
    any parent is a work tree that can't be read: ConfigError, never a copy without its
    gitignore."""
    ran = await _local(directory, "rev-parse", "--is-inside-work-tree")
    if ran.exit_code == _NO_GIT:
        if _dot_git_above(directory):
            raise invalid(
                f"{label} is inside a git repository and git isn't on this host's PATH; "
                "install git, or point localDir outside the repository"
            )
        return False
    return ran.exit_code == 0 and ran.stdout.strip() == "true"


def _listed(ran: Git, what: str, label: str) -> list[str]:
    if ran.exit_code != 0:
        raise invalid(f"{label}: git {what} failed: {ran.stderr}")
    return [p for p in ran.stdout.split("\0") if p]


async def ignored_paths(directory: Path, label: str) -> frozenset[str]:
    """The paths git ignores under `directory`, relative to it; a directory without its `/`."""
    ran = await _local(
        directory, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"
    )
    return frozenset(p.removesuffix("/") for p in _listed(ran, "ls-files", label))


async def submodules(directory: Path, label: str) -> frozenset[str]:
    """The submodules (gitlinks, mode 160000) git tracks under `directory`, relative to it."""
    ran = await _local(directory, "ls-files", "-z", "--stage")
    return frozenset(
        line.split("\t", 1)[1]
        for line in _listed(ran, "ls-files", label)
        if line.startswith("160000 ") and "\t" in line
    )


@asynccontextmanager
async def checkout(
    url: Callable[[str], str], repo: str, ref: str, token: str | None
) -> AsyncGenerator[tuple[Path, str]]:
    """Fetches `ref` of `repo` on the host (with the forge credential when there is one) into a
    scratch checkout and yields it with its commit, then removes it. The checkout's own `.git`
    holds no credential: the header rides in the environment."""
    label = f"workspace git {repo}@{ref}"
    if not _REPO.match(repo) or ".." in repo:
        raise invalid(f"{label}: repo must be owner/name")
    if not _REF.match(ref) or ".." in ref:
        raise invalid(f"{label}: ref must be a plain branch, tag or commit name")
    remote = url(repo)
    with tempfile.TemporaryDirectory(prefix="threads-git-") as scratch:
        home = Path(scratch)
        env = credential_env(token, home)

        async def step(cwd: Path, *args: str) -> str:
            ran = await git(args, cwd, env)
            if ran.exit_code != 0:
                raise invalid(f"{label}: git {' '.join(args)} failed: {ran.stderr}")
            return ran.stdout

        work = home / "w"
        work.mkdir()
        await step(work, "init", "--quiet")
        await step(work, "fetch", "--quiet", "--depth", "1", remote, ref)
        await step(work, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        commit = (await step(work, "rev-parse", "HEAD")).strip()
        yield work, commit

"""One host directory per test, with the files a real repository has around a workspace input."""

import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

GIT = shutil.which("git") or "git"
HAS_GIT = shutil.which("git") is not None


def dir_of(files: Mapping[str, str], root: Path) -> Path:
    """Writes `files` (path -> contents) under `root`; a path ending `*` gets mode 0o755."""
    for raw, body in files.items():
        executable = raw.endswith("*")
        path = root / (raw[:-1] if executable else raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755 if executable else 0o644)
    return root


def git(directory: Path, *args: str) -> None:
    # The arguments are this test file's own literals.
    subprocess.run(  # noqa: S603
        [GIT, *args], cwd=directory, check=True, capture_output=True
    )


def repo_of(files: Mapping[str, str], root: Path) -> Path:
    """A git work tree, so `git ls-files` answers."""
    dir_of(files, root)
    git(root, "init", "--quiet")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")
    return root


def scratch() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="threads-ws-")

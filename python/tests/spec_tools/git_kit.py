"""Throwaway git repositories for the surface gate's base-commit tests."""

import json
import pathlib
import subprocess

from check_api import Json

IDENTITY = ("-c", "user.name=surface test", "-c", "user.email=surface@test.invalid")


def git(repo: pathlib.Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv in a test
        ["git", *IDENTITY, *args],  # noqa: S607 - git from PATH
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def write_json(path: pathlib.Path, doc: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def commit(repo: pathlib.Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init(repo: pathlib.Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")

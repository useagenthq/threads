"""The git gateway's host side: git runs on the host with the forge credential,
passed as an HTTP header through git's environment config, never in argv, a URL or the sandbox.
Repositories move to and from the sandbox as git bundles."""

import asyncio
import base64
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

TIMEOUT_S: Final = 300.0


@dataclass(frozen=True, slots=True)
class Git:
    exit_code: int
    stdout: str
    stderr: str


def credential_env(token: str | None, home: Path) -> Mapping[str, str]:
    """A clean environment: no user or system git config, no prompts, and the token (if any)
    as an Authorization header for the forge's HTTPS remote."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if token is not None:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    return env


async def git(args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Git:
    """One host git command. A timeout kills it and reads as exit code -1."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(TIMEOUT_S):
            out, err = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return Git(-1, "", "git timed out")
    code = proc.returncode if proc.returncode is not None else -1
    return Git(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

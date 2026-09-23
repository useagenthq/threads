"""spec/api.json `SandboxSession.exec`: what every session does before its first await. The call
is copied, so what runs is what was checked even if the caller's objects change mid-call, and a
caller bug raises before anything reaches the sandbox."""

import re
from collections.abc import Mapping, Sequence
from typing import Final

_SHELL_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def admit_exec(
    command: Sequence[str], env: Mapping[str, str]
) -> tuple[tuple[str, ...], Mapping[str, str]]:
    """The command and env, copied and checked. An empty command, an env name that is not a
    shell variable name, or one the sandbox-side wrapper keeps for itself (`__t_`), is a caller
    bug. (`cwd`, `process_key` and `stdin` are immutable in Python and need no copy.)"""
    command, env = tuple(command), dict(env)
    if not command:
        raise ValueError("exec needs a command")
    names = sorted(env)
    bad = [name for name in names if _SHELL_NAME.fullmatch(name) is None]
    if bad:
        raise ValueError(f"env name is not a shell name: {bad}")
    reserved = [name for name in names if name.startswith("__t_")]
    if reserved:
        raise ValueError(f"env names starting __t_ are reserved for the sandbox: {reserved}")
    return command, env

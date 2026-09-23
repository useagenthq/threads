"""Skills (spec/api.json `Skill`): host config, pinned by hash at
thread start. Only what the host passes to `agent(skills=...)` is a skill; a skill-shaped file in
the sandbox or repo is data and never loads as one (F4.3). Line 0 lists each name and
description; load_skill appends a body as a trusted `injected{source: skill}` (F4.1)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from threads._generated.tools_v1 import LoadSkillInput
from threads.agents.config import ConfigError
from threads.log import JsonObject, ToolSpec
from threads.log.digest import sha256_hex
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.tools import Dispatched, Invocation, Output, Reference, Termination
from threads.result import Err
from threads.tools.runner import parse

_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""events.v1 `$defs/Name`."""


@dataclass(frozen=True, slots=True)
class Skill:
    """spec/api.json `Skill`: listed by name and description; the body loads on demand."""

    name: str
    description: str
    body: str

    @property
    def version(self) -> str:
        """The SHA-256 of the body: what config_hash pins and the injection's origin names."""
        return sha256_hex(self.body.encode("utf-8"))


def checked(skills: Sequence[Skill]) -> tuple[Skill, ...]:
    """Raises ConfigError for a name that isn't a wire name or repeats, or a description that
    isn't one non-empty line."""
    for s in skills:
        if _NAME.fullmatch(s.name) is None:
            raise ConfigError("invalid_config", f"skill name {s.name!r} isn't a wire name")
        if not s.description.strip() or "\n" in s.description:
            raise ConfigError("invalid_config", f"skill {s.name}: one non-empty description line")
    names = [s.name for s in skills]
    if len(set(names)) != len(names):
        raise ConfigError("duplicate_name", f"skill names repeat: {names}")
    return tuple(skills)


def listing(skills: Sequence[Skill]) -> str:
    """The line-0 listing: names and descriptions only, never a body."""
    if not skills:
        return ""
    lines = (f"- {s.name}: {s.description}" for s in skills)
    return "Skills you can load with load_skill:\n" + "\n".join(lines)


def pinned(skills: Sequence[Skill]) -> list[JsonValue]:
    """What config_hash pins for each skill: a changed body is a changed config."""
    return [{"name": s.name, "description": s.description, "sha256": s.version} for s in skills]


class SkillLoader:
    """load_skill: host-side and read_only over the skills pinned at thread start."""

    def __init__(self, skills: Sequence[Skill]) -> None:
        self._skills = {s.name: s for s in skills}

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        parsed = parse(spec.name, input)
        return parsed.error if isinstance(parsed, Err) else None

    async def dispatch(self, call: Invocation) -> Dispatched:
        parsed = parse(call.spec.name, call.input)
        if isinstance(parsed, Err) or not isinstance(parsed.value, LoadSkillInput):
            raise AssertionError("a dispatched call was parsed first")
        name = parsed.value.name
        skill = self._skills.get(name)
        if skill is None:
            return Output(f"not_found: no skill named {name}; only the listed skills exist", True)
        ref = Reference("skill", skill.name, skill.version, skill.body)
        return Output(f"loaded skill {name}", False, None, (ref,))

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return LookupUnknown("load_skill has no lookup")

    async def terminate(self, call: Invocation) -> Termination:
        return "unknown"

    def provider_now(self) -> int | None:
        return None

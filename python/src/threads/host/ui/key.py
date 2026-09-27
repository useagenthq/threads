"""A browser's chat key names one thread per (principal, agent, key), by derivation: no lookup
table, and the key itself is never recorded (spec/schema/ui/README.md, "Chat key").
thread_id = UUIDv8 of sha256(lp("threads-ui-v1") || lp(principalKey) || lp(agent) || lp(key)).

The derivation itself is ../derive.py, shared with A2A's context so the two cannot drift apart."""

import re
from typing import Final

from threads.host.derive import derived_thread_id
from threads.log import Principal, ThreadId
from threads.log.keys import principal_key

CHAT_KEY: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

DOMAIN: Final = "threads-ui-v1"
"""What keeps a browser chat's threads apart from every other derived surface's."""


def ui_thread_id(principal: Principal, agent: str, key: str) -> ThreadId:
    return derived_thread_id(DOMAIN, (principal_key(principal), agent, key))

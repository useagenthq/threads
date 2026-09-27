"""A browser's chat key names one thread per (principal, agent, key), by derivation: no lookup
table, and the key itself is never recorded (spec/schema/ui/README.md, "Chat key").
thread_id = UUIDv8 of sha256(lp("threads-ui-v1") || lp(principalKey) || lp(agent) || lp(key))."""

import re
from typing import Final

from threads.log import Principal, ThreadId
from threads.log.derive import uuid_v8
from threads.log.keys import principal_key

CHAT_KEY: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def ui_thread_id(principal: Principal, agent: str, key: str) -> ThreadId:
    return ThreadId(uuid_v8("threads-ui-v1", principal_key(principal), agent, key))

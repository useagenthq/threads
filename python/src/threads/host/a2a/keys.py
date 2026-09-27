"""The receipt key, the context-to-thread derivation, and the ids the exposed side derives rather
than generates. Nothing here is random: a frame, a task id and a status message must be the same on
every read of the same committed log, in both languages. spec/conformance/vectors/a2a.json pins the
three that two hosts on one store must agree on.

The receipt's operation literal lives with the other operations, in ../a2a/receipts.py."""

from collections.abc import Mapping
from typing import Final

from pydantic import JsonValue

from threads.a2a.protocol import PROVENANCE
from threads.host.derive import derived_id, derived_thread_id, derived_uuid
from threads.log import Principal, ThreadId
from threads.log.digest import canonical_sha256
from threads.log.keys import principal_key
from threads.result import Err

MAX_MESSAGE_ID_BYTES: Final = 255
"""A messageId over this many UTF-8 bytes is refused: it would not fit the key readably."""

DOMAIN: Final = "threads-a2a-v1"
"""What keeps an A2A context's threads apart from a browser chat's, which must never collide."""

MAX_HOPS: Final = 8
"""The hop count at which a call chain is refused. A liar can only use it against itself."""


def send_key(principal: Principal, agent: str, message_id: str) -> str:
    """`"a2a/"` then, for each of principal_key, the agent and the messageId, its UTF-8 byte
    length, `":"` and the value. Length-prefixed, so no two field splits collide, and the principal
    is in the key, so two callers' equal messageIds never collide."""
    fields = (principal_key(principal), agent, message_id)
    return "a2a/" + "".join(f"{len(f.encode())}:{f}" for f in fields)


def message_id_too_long(message_id: str) -> bool:
    """Whether a messageId can be keyed at all; the refusal is InvalidParamsError at the route."""
    return len(message_id.encode()) > MAX_MESSAGE_ID_BYTES


def a2a_thread_id(principal: Principal, agent: str, context_id: str) -> ThreadId:
    """The thread a context names. A separate domain string from the UI's chat key is required: an
    A2A context must never collide with a browser chat, and the derivation is one-way, so a caller
    cannot reach another caller's thread by guessing a contextId either."""
    return derived_thread_id(DOMAIN, (principal_key(principal), agent, context_id))


def body_hash(agent: str, message: JsonValue) -> str | None:
    """sha256 of the canonical JSON of `{agent, message}`: what a reused messageId is checked
    against.

    The message hashed is the caller's **own JSON**, not the parsed model. Two hosts on one store
    may be a TypeScript one and a Python one, and they must agree on this hash or the same
    messageId would look like two different messages. Hashing the bytes the caller sent makes them
    agree by construction, instead of by both dropping absent optional fields the same way."""
    hashed = canonical_sha256({"agent": agent, "message": message})
    return None if isinstance(hashed, Err) else hashed.value


def raw_message(params: JsonValue) -> JsonValue:
    """The caller's `message` as it arrived, for hashing. Absent or not JSON is None."""
    return params.get("message") if isinstance(params, dict) else None


def status_message_id(task_id: str, seq: int) -> str:
    """A status message's id, derived from the task and the seq it is read at, so the same committed
    state always renders the same message. Both fields are fixed-width, so no length prefix."""
    return derived_uuid("threads/a2a-status", (task_id, str(seq)))


def artifact_id(task_id: str) -> str:
    """A completed task's one artifact id, derived from the task."""
    return derived_uuid("threads/a2a-artifact", (task_id,))


def rejected_task_id(principal: Principal, agent: str, message_id: str) -> str:
    """The id of a task we refuse before it exists (a claimed hop count too deep). Derived from the
    request, so a retry of the same message is answered the same way and nothing is stored."""
    return derived_id("threads-a2a-rejected-v1", (principal_key(principal), agent, message_id))


def claims(metadata: Mapping[str, JsonValue] | None) -> Mapping[str, JsonValue] | None:
    """The caller's provenance claim, recorded as an untrusted claim and nothing else: it grants no
    authority, picks no budget and never becomes the run's provenance.principal. A claim that is not
    an object at all is ignored rather than refused: a partner's metadata is not a contract."""
    found = None if metadata is None else metadata.get(PROVENANCE)
    return found if isinstance(found, dict) else None


def claimed_hops(claim: Mapping[str, JsonValue] | None) -> int | None:
    """A claimed hop count, when the claim carries a usable one."""
    if claim is None:
        return None
    hops = claim.get("hops")
    return hops if isinstance(hops, int) and not isinstance(hops, bool) and hops >= 0 else None

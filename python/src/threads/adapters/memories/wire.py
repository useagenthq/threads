"""What a hosted memory adapter stores beside each record, and reads back through a strict model:
the host's binding and the origin, opaque to the provider."""

import hashlib
from collections.abc import Mapping

from pydantic import ValidationError

from threads._strict_model import StrictModel
from threads.memory.types import Binding, MemoryHit, Origin, Scope


class Tagged(StrictModel):
    namespace: str
    record_id: str
    origin: Origin


def container(scope: Scope) -> str:
    """The provider-side container of one scope: a digest, so no scope text reaches it."""
    raw = "\x00".join((scope.tenant_id, scope.agent, scope.scope)).encode("utf-8")
    return "threads_" + hashlib.sha256(raw).hexdigest()[:40]


def record_id(key: str) -> str:
    """A provider-side id derived from the write key, so a retried write names the same record."""
    return "threads_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]


def hit(id: str, text: str, score: float, metadata: object) -> MemoryHit | None:
    """A provider item as a hit, or None when its metadata isn't ours (it can't prove a binding)."""
    if not isinstance(metadata, Mapping):
        return None
    ours = {k: v for k, v in metadata.items() if k in Tagged.model_fields}  # pyright: ignore[reportUnknownVariableType] - checked by Tagged below
    try:
        tag = Tagged.model_validate(ours)
    except ValidationError:
        return None
    binding = Binding(namespace=tag.namespace, record_id=tag.record_id)
    return MemoryHit(id=id, version="1", text=text, score=score, origin=tag.origin, binding=binding)
